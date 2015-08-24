import string

import numpy
import six

import cupy
from cupy import carray
from cupy import cuda
from cupy import util


@util.memoize(for_each_device=True)
def _get_simple_elementwise_kernel(
        params, operation, name='kernel', preamble='',
        loop_prep='', after_loop='', options=()):
    module_code = string.Template('''
    ${preamble}
    extern "C" __global__ void ${name}(${params}) {
      ${loop_prep};
      CUPY_FOR(i, _ind.size()) {
        _ind.set(i);
        ${operation};
      }
      ${after_loop};
    }
    ''').substitute(
        params=params,
        operation=operation,
        name=name,
        preamble=preamble,
        loop_prep=loop_prep,
        after_loop=after_loop)
    module = carray.compile_with_cache(module_code, options)
    return module.get_function(name)


def _get_ndarray_dtype(args):
    return tuple(a.dtype if isinstance(a, cupy.ndarray) else None
                 for a in args)
_typenames = {
    numpy.dtype('float64'): 'double',
    numpy.dtype('float32'): 'float',
    numpy.dtype('float16'): 'float16',
    numpy.dtype('int64'): 'long long',
    numpy.dtype('int32'): 'int',
    numpy.dtype('int16'): 'short',
    numpy.dtype('int8'): 'signed char',
    numpy.dtype('uint64'): 'unsigned long long',
    numpy.dtype('uint32'): 'unsigned int',
    numpy.dtype('uint16'): 'unsigned short',
    numpy.dtype('uint8'): 'unsigned char',
    numpy.dtype('bool'): 'bool',
}


_scalar_type = (int, float, bool) + tuple(t.type for t in _typenames.keys())


def _get_typename(dtype):
    if dtype is None:
        raise ValueError('dtype is None')
    return _typenames[numpy.dtype(dtype)]


def _check_args(args):
    dev = cuda.Device()
    for arg in args:
        if isinstance(arg, cupy.ndarray):
            arg_dev = arg.data.device
            if arg_dev == dev:
                continue
            raise ValueError('Array device must be same as the current '
                             'device: array device = %d while current = %d'
                             % (arg_dev.id, dev.id))
        if isinstance(arg, _scalar_type):
            continue
        raise TypeError('Unsupported type %s' % type(arg))


def _get_args_info(args):
    return tuple([(type(a), getattr(a, 'dtype', None), a.ndim) for a in args])


def _get_kernel_params(params, args_info):
    ret = []
    for p, a in six.moves.zip(params, args_info):
        type, dtype, ndim = a
        is_array = type is cupy.ndarray
        if type is carray.Indexer:
            t = 'CIndexer<{}>'.format(ndim)
        else:
            t = _get_typename(dtype)
            if is_array:
                t = 'CArray<{}, {}>'.format(t, ndim)
        ret.append('{}{} {}{}'.format(
            'const ' if p.is_const else '', t,
            '_raw_' if is_array and not p.raw else '', p.name))
    return ', '.join(ret)


def _reduce_dims(args, params, indexer):
    if indexer.ndim <= 1:
        return list(args), indexer
    is_array_flags = [not p.raw and isinstance(a, cupy.ndarray)
                      for a, p in six.moves.zip(args, params)]
    array_args = [a for a, f in six.moves.zip(args, is_array_flags) if f]
    shape = list(indexer.shape)
    for i in six.moves.range(1, len(shape)):
        for arg in array_args:
            strides = arg.strides
            if strides[i] * shape[i] != strides[i - 1]:
                break
        else:
            shape[i] *= shape[i - 1]
            shape[i - 1] = 1

    new_shape = tuple(dim for dim in shape if dim != 1)
    if new_shape == indexer.shape:
        return list(args), indexer

    new_args = list(args)
    for i in six.moves.range(len(args)):
        if is_array_flags[i]:
            arg = args[i].view()
            new_strides = tuple(s for i, s in enumerate(arg.strides)
                                if shape[i] != 1)
            arg._shape = new_shape
            arg._strides = new_strides
            new_args[i] = arg

    indexer.shape = new_shape
    return new_args, indexer


def _get_inout_args(args, indexer, params, reduce_dims):
    if reduce_dims:
        args, indexer = _reduce_dims(args, params, indexer)
    args.append(indexer)
    return args


class ParameterInfo(object):

    def __init__(self, str, is_const):
        self.name = None
        self.dtype = None
        self.ctype = None
        self.raw = False
        self.is_const = is_const
        s = tuple(i for i in str.split() if len(i) != 0)
        if len(s) < 2:
            raise Exception('Syntax error: %s' % str)

        t, self.name = s[-2:]
        if t == 'CIndexer':
            pass
        elif len(t) == 1:
            self.ctype = t
        else:
            self.dtype = numpy.dtype(t)
            if self.dtype.name != t:
                raise ValueError('Wrong type %s' % t)
            self.ctype = _get_typename(self.dtype)

        for i in s[:-2]:
            if i == 'raw':
                self.raw = True
            else:
                raise Exception('Unknown keyward "%s"' % i)


@util.memoize()
def _get_param_info(s, is_const=False):
    if len(s) == 0:
        return ()
    return tuple(ParameterInfo(i, is_const) for i in s.strip().split(','))


@util.memoize(for_each_device=True)
def _decide_params_type(in_params, out_params, in_args_dtype, out_args_dtype):
    type_dict = {}
    if out_args_dtype:
        assert len(out_params) == len(out_args_dtype)
        for p, a in six.moves.zip(out_params, out_args_dtype):
            if a is None:
                raise TypeError('Output arguments must be cupy.ndarray')
            if p.dtype is not None:
                if a != p.dtype:
                    raise TypeError(
                        'Type is mismatched %s', (p.name, a, p.dtype))
            elif p.ctype in type_dict:
                t = type_dict[p.ctype]
                if t != a:
                    raise TypeError(
                        'Type is mismatched %s', (p.name, a, t))
            else:
                type_dict[p.ctype] = a

    assert len(in_params) == len(in_args_dtype)
    unknown_ctype = []
    for p, a in six.moves.zip(in_params, in_args_dtype):
        if a is None:
            if p.dtype is None:
                unknown_ctype.append(p.ctype)
        else:
            if p.dtype is not None:
                if a != p.dtype:
                    raise TypeError(
                        'Type is mismatched %s', (p.name, a, p.dtype))
            elif p.ctype in type_dict:
                t = type_dict[p.ctype]
                if t != a:
                    raise TypeError(
                        'Type is mismatched %s' % (p.name, a, t, p.ctype))
            else:
                type_dict[p.ctype] = a

    in_types = tuple(p.dtype if p.dtype is not None else type_dict[p.ctype]
                     for p in in_params)
    out_types = tuple(p.dtype if p.dtype is not None else type_dict[p.ctype]
                      for p in out_params)
    return in_types, out_types, tuple(type_dict.items())


def _broadcast(args, params, size_error=True):
    brod = cupy.broadcast(
        *[a if not p.raw and isinstance(a, cupy.ndarray) else None
          for p, a in six.moves.zip(params, args)])
    if size_error and all(i is None for i in brod.values):
        raise ValueError('Loop size is Undecided')
    return brod, tuple(b if a is None else a
                       for a, b in six.moves.zip(brod.values, args))


def _get_out_args(in_args, out_args, out_types, out_shape, out_params=None):
    if len(out_args) == 0:
        if out_params is not None and any(p.raw for p in out_params):
            raise ValueError('Output array size is Undecided')
        out_args = tuple(cupy.empty(shape=out_shape, dtype=t)
                         for t in out_types)
    else:
        for i, a in enumerate(out_args):
            if not isinstance(a, cupy.ndarray):
                raise TypeError(
                    'Output arguments type must be cupy.ndarray')
            if a.shape != out_shape:
                if out_params is None or not out_params[i].raw:
                    raise ValueError('Out shape is mismatched')
    return out_args


@util.memoize(for_each_device=True)
def _get_elementwise_kernel(
        args_info, types, params, operation, name,
        preamble, **kwargs):
    kernel_params = _get_kernel_params(params, args_info)
    types_preamble = '\n'.join(
        'typedef {} {};'.format(_get_typename(v), k) for k, v in types)
    preamble = types_preamble + '\n' + preamble

    op = []
    for p, a in six.moves.zip(params, args_info):
        if p.raw or a[0] != cupy.ndarray:
            continue
        if p.is_const:
            fmt = 'const {t} {n} = _raw_{n}[_ind.get()];'
        else:
            fmt = '{t} &{n} = _raw_{n}[_ind.get()];'
        op.append(fmt.format(t=p.ctype, n=p.name))
    op.append(operation)
    operation = '\n'.join(op)
    return _get_simple_elementwise_kernel(
        kernel_params, operation, name,
        preamble, **kwargs)


class ElementwiseKernel(object):

    """User-defined elementwise kernel.

    This class can be used to define an elementwise kernel with or without
    broadcasting.

    The kernel is compiled at an invocation of the
    :meth:`~ElementwiseKernel.__call__` method,
    which is cached for each device.
    The compiled binary is also cached into a file under the
    ``$HOME/.cupy/kernel_cache/`` directory with a hashed file name. The cached
    binary is reused by other processes.

    Args:
        in_params (str): Input argument list.
        out_params (str): Output argument list.
        operation (str): The body in the loop written in CUDA-C/C++.
        name (str): Name of the kernel function. It should be set for
            readability of the performance profiling.
        reduce_dims (bool): If False, the shapes of array arguments are
            kept within the kernel invocation. The shapes are reduced
            (i.e., the arrays are reshaped without copy to the minimum
            ndims) by default. It may make the kernel fast by reducing the
            index calculations.
        options (list): Options passed to the nvcc command.
        preamble (str): Fragment of the CUDA-C/C++ code that is inserted at the
            top of the cu file.
        loop_prep (str): Fragment of the CUDA-C/C++ code that is inserted at
            the top of the kernel function definition and above the ``for``
            loop.
        after_loop (str): Fragment of the CUDA-C/C++ code that is inserted at
            the bottom of the kernel function definition.

    """
    def __init__(self, in_params, out_params, operation,
                 name='kernel', reduce_dims=True, preamble='', **kwargs):
        self.in_params = _get_param_info(in_params, True)
        self.out_params = _get_param_info(out_params)
        self.nin = len(self.in_params)
        self.nout = len(self.out_params)
        param_rest = _get_param_info('CIndexer _ind')
        self.params = self.in_params + self.out_params + param_rest
        self.operation = operation
        self.name = name
        self.reduce_dims = reduce_dims
        self.preamble = preamble
        self.kwargs = kwargs
        names = [p.name for p in self.in_params + self.out_params]
        if 'i' in names:
            raise ValueError("Can not use 'i' as a parameter name")

    def __call__(self, *args, **kwargs):
        """Compiles and invokes the elementwise kernel.

        The compilation runs only if the kernel is not cached. Note that the
        kernels with different argument dtypes or ndims are not compatible. It
        means that single ElementwiseKernel object may be compiled into
        multiple kernel binaries.

        Args:
            args: Argumens of the kernel.
            size (int): Range size of the indices. If specified, the variable
                ``n`` is set to this value. Otherwise, the result of
                broadcasting is used to determine the value of ``n``.

        Returns:
            Arrays are returned according to the ``out_params`` argument of the
            ``__init__`` method.

        """
        n = kwargs.pop('size', None)

        if not (len(args) == self.nin or
                len(args) == self.nin + self.nout):
            raise TypeError('Wrong number of arguments for %s' % self.name)
        for i in args:
            if isinstance(i, numpy.ndarray):
                raise TypeError('Unsupported type %s' % type(i))
        _check_args(args)

        brod, value = _broadcast(args, self.params, n is None)
        in_args = value[:self.nin]
        out_args = value[self.nin:]
        in_types, out_types, types = _decide_params_type(
            self.in_params, self.out_params,
            _get_ndarray_dtype(in_args), _get_ndarray_dtype(out_args))

        in_args = tuple(x if isinstance(x, cupy.ndarray) else t.type(x)
                        for x, t in six.moves.zip(in_args, in_types))
        out_args = _get_out_args(
            in_args, out_args, out_types, brod.shape, self.out_params)

        ret = out_args
        if len(ret) == 1:
            ret = ret[0]

        if n is None:
            indexer = carray.Indexer(brod.shape)
        else:
            indexer = carray.Indexer((n,))

        if brod.size == 0:
            return ret

        inout_args = _get_inout_args(
            in_args + out_args, indexer, self.params, self.reduce_dims)
        args_info = _get_args_info(inout_args)
        kern = _get_elementwise_kernel(
            args_info, types, self.params, self.operation,
            self.name, self.preamble, **self.kwargs)
        kern.linear_launch(indexer.size, inout_args)
        return ret


@util.memoize(for_each_device=True)
def _get_ufunc_kernel(in_types, out_types, routine, args_info, out_raw_types,
                      params, name, preamble):
    kernel_params = _get_kernel_params(params, args_info)

    types = []
    op = []
    for i, x in enumerate(in_types):
        types.append('typedef {} in{}_type;'.format(_get_typename(x), i))
        if args_info[i][0] is not cupy.ndarray:
            continue
        op.append(
            'const in{0}_type in{0} = _raw_in{0}[_ind.get()];'.format(i))

    for i, x in enumerate(out_types):
        types.append('typedef {} out{}_type;'.format(_get_typename(x), i))
        op.append('{1} &out{0} = _raw_out{0}[_ind.get()];'.format(
            i, _get_typename(out_raw_types[i])))

    op.append(routine)
    operation = '\n'.join(op)

    types.append(preamble)
    preamble = '\n'.join(types)

    return _get_simple_elementwise_kernel(
        kernel_params, operation, name, preamble)


@util.memoize()
def _castable(src, tgt):
    return numpy.can_cast(src, tgt)


def _can_cast(arg, totype):
    totype = numpy.dtype(totype).char
    return _castable(arg.dtype.char, totype)


class ufunc(object):

    """Universal function.

    Attributes:
        name (str): The name of the universal function.
        nin (int): Number of input arguments.
        nout (int): Number of output arguments.
        nargs (int): Number of all arguments.

    """
    def __init__(self, name, nin, nout, ops, preamble='', doc=''):
        self.name = name
        self.nin = nin
        self.nout = nout
        self.nargs = nin + nout
        self._ops = ops
        self._preamble = preamble
        self.__doc__ = doc
        _in_params = tuple(
            ParameterInfo('T in{}'.format(i), True)
            for i in six.moves.range(nin))
        _out_params = tuple(
            ParameterInfo('T out{}'.format(i), False)
            for i in six.moves.range(nout))
        self._params = _in_params + _out_params + (
            ParameterInfo('CIndexer _ind', False),)
        self._routine_table = {}

    def __repr__(self):
        return "<ufunc '%s'>" % self.name

    @property
    def types(self):
        """A list of type signatures.

        Each type signature is represented by type character codes of inputs
        and outputs separated by '->'.

        """
        types = []
        for in_types, out_types, _ in self._ops:
            in_str = ''.join(t.char for t in in_types)
            out_str = ''.join(t.char for t in out_types)
            types.append('{}->{}'.format(in_str, out_str))
        return types

    def __call__(self, *args, **kwargs):
        """Applies the universal function to arguments elementwise.

        Args:
            args: Input arguments. Each of them can be a cupy.ndarray object or
                a scalar. The output arguments can be omitted or be specified
                by the ``out`` argument.
            out (cupy.ndarray): Output array. It outputs to new arrays
                default.
            dtype: Data type specifier.

        Returns:
            Output array or a tuple of output arrays.

        """
        out = kwargs.get('out', None)
        dtype = kwargs.get('dtype', None)
        if dtype is not None:
            dtype = numpy.dtype(dtype)

        if not (len(args) == self.nin or len(args) == self.nargs):
            raise TypeError('Wrong number of arguments for %s' % self.name)

        brod = cupy.broadcast(*args)
        in_args = tuple(numpy.dtype(type(i)).type(i)
                        if isinstance(i, (int, float, bool)) else i
                        for i in brod.values[:self.nin])
        out_args = args[self.nin:]
        if out is not None:
            if len(out_args) != 0:
                raise ValueError("cannot specify 'out' as both "
                                 "a positional and keyword argument")
            out_args = out,
        _check_args(in_args + out_args)

        in_types, out_types, routine = self._guess_routine(in_args, dtype)

        in_args = tuple(x if isinstance(x, cupy.ndarray) else t.type(x)
                        for x, t in six.moves.zip(in_args, in_types))
        out_args = _get_out_args(in_args, out_args, out_types, brod.shape)

        ret = out_args
        if len(ret) == 1:
            ret = ret[0]

        if 0 in brod.shape:
            return ret

        indexer = carray.Indexer(brod.shape)
        inout_args = _get_inout_args(
            in_args + out_args, indexer, self._params, True)
        args_info = _get_args_info(inout_args)
        out_raw_types = tuple(x.dtype for x in out_args)
        kern = _get_ufunc_kernel(
            in_types, out_types, routine,
            args_info, out_raw_types,
            self._params, self.name, self._preamble)

        kern.linear_launch(indexer.size, inout_args)
        return ret

    def _guess_routine_from_in_types(self, in_types):
        for op in self._ops:
            if all(numpy.can_cast(t0, t1) for t0, t1
                   in six.moves.zip(in_types, op[0])):
                return op
        return None

    def _guess_routine_from_dtype(self, dtype):
        for op in self._ops:
            if all(t == dtype for t in op[1]):
                return op
        return None

    def _guess_routine(self, in_args, dtype):
        if dtype is None:
            key = tuple(i.dtype for i in in_args)
        else:
            key = dtype
        op = self._routine_table.get(key, ())
        if op is ():
            if dtype is None:
                op = self._guess_routine_from_in_types(key)
            else:
                op = self._guess_routine_from_dtype(key)
            self._routine_table[key] = op
        if op is not None:
            return op
        raise TypeError('Wrong type of arguments for %s' % self.name)


def create_ufunc(name, ops, routine=None, preamble='', doc=''):
    _ops = []
    for t in ops:
        if not isinstance(t, tuple):
            typ = t
            rt = routine
        else:
            typ, rt = t

        types = typ.split('->')
        if len(types) == 1:
            in_types = out_types = tuple(types)
        else:
            in_types, out_types = map(tuple, types)
        in_types = tuple(numpy.dtype(t) for t in in_types)
        out_types = tuple(numpy.dtype(t) for t in out_types)
        _ops.append((in_types, out_types, rt))

    return ufunc(name, len(_ops[0][0]), len(_ops[0][1]), _ops, preamble, doc)


_id = 'out0 = in0'

copy = create_ufunc(
    'cupy_copy',
    ['?->?', 'b->b', 'B->B', 'h->h', 'H->H', 'i->i', 'I->I', 'l->l', 'L->L',
     'q->q', 'Q->Q', 'e->e', 'f->f', 'd->d'],
    _id)


copy_where = create_ufunc(
    'cupy_copy_where',
    ['??->?', 'b?->b', 'B?->B', 'h?->h', 'H?->H', 'i?->i', 'I?->I', 'l?->l',
     'L?->L', 'q?->q', 'Q?->Q', 'e?->e', 'f?->f', 'd?->d'],
    'if (in1) out0 = in0')


_divmod = create_ufunc(
    'cupy_divmod',
    ['bb->b', 'BB->B', 'hh->h', 'HH->H', 'ii->i', 'II->I', 'll->l', 'LL->L',
     'qq->q', 'QQ->Q', 'ee->e', 'ff->f', 'dd->d'],
    'out0_type a = _floor_divide(in0, in1); out0 = a; out1 = in0 - a * in1')

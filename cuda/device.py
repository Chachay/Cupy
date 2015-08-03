import atexit
import collections

import six

from cupy.cuda import cublas
from cupy.cuda import runtime


class Device(object):

    """Object that represents a CUDA device.

    This class provides some basic manipulations on CUDA devices.

    It supports the context protocol. For example, the following code is an
    example of temporarily switching the current device::

       with Device(0):
           do_something_on_device_0()

    After the *with* statement gets done, the current device is reset to the
    original one.

    Args:
        device (int or cupy.cuda.Device): Index of the device to manipulate. Be
            careful that the device ID (a.k.a. GPU ID) is zero origin. If it is
            a Device object, then its ID is used. The current device is
            selected by default.

    Attributes:
        id (int): ID of this device.

    """
    _cublas_handles = {}

    def __init__(self, device=-1):
        if isinstance(device, Device):
            self.id = device.id
        elif device < 0:
            self.id = runtime.getDevice()
        else:
            self.id = device

        self._device_stack = []

    def __enter__(self):
        self._device_stack.append(Device())
        self.use()

    def __exit__(self, *args):
        device = self._device_stack.pop()
        device.use()

    def use(self):
        """Makes this device current.

        If you want to switch a device temporarily, use the
        :func:`using_device` function with ``with`` statement, instead.

        """
        runtime.setDevice(self.id)

    @staticmethod
    def synchronize():
        """Synchronizes the current thread to the current device."""
        runtime.deviceSynchronize()

    @property
    def compute_capability(self):
        """Compute capability of this device.

        The capability is represented by a string containing the major index
        and the minor index. For example, compute capability 3.5 is represented
        by the string '35'.

        """
        major = runtime.deviceGetAttribute(75, self.id)
        minor = runtime.deviceGetAttribute(76, self.id)
        return '%d%d' % (major, minor)

    @property
    def cublas_handle(self):
        """The cuBLAS handle for this device.

        The same handle is used for the same device even if the Device instance
        itself is different.

        """
        handle = self._cublas_handles.get(self.id, None)
        if handle is None:
            with using_device(self):
                handle = cublas.create()
                self._cublas_handles[self.id] = handle
        return handle

    def __eq__(self, other):
        """Returns True if ``other`` refers to the same device."""
        if not isinstance(other, Device):
            return False
        return self.id == other.id

    def __ne__(self, other):
        """Returns True if ``other`` refers to a different device."""
        return not (self == other)


def from_pointer(ptr):
    """Extracts a Device object from a device pointer.

    Args:
        ptr (ctypes.c_void_p): Pointer to the device memory.

    Returns:
        Device: The device whose memory the pointer refers to.

    """
    attrs = runtime.pointerGetAttributes(ptr)
    return Device(attrs.device)


@atexit.register
def destroy_cublas_handles():
    """Destroys the cuBLAS handles for all devices."""
    for handle in six.itervalues(Device._cublas_handles):
        cublas.destroy(handle)
    Device._cublas_handles = {}


_memoized_funcs = []


def memoize(f):
    """Makes a function memoizing the result for each argument and device.

    This decorator provides per-device memoizing of the function result.

    """
    def func(*args, **kwargs):
        # TODO(okuta): Improve keyword arguments.
        global _memoized_funcs

        if not hasattr(f, '_cupy_dev_memo'):
            _memoized_funcs.append(f)
            f._cupy_dev_memo = collections.defaultdict(dict)

        memo = f._cupy_dev_memo[Device().id]
        arg_key = (args, frozenset(kwargs.items()))
        result = memo.get(arg_key, None)
        if result is None:
            result = f(*args, **kwargs)
            memo[arg_key] = result
        return result

    return func


@atexit.register
def clear_device_dependent_memo():
    """Clears the memoized results for all functions decorated by memoize."""
    global _memoized_funcs
    for func in _memoized_funcs:
        del func._cupy_dev_memo
    _memoized_funcs = []

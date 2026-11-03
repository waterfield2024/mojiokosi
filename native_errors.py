"""Return native startup failures to the app instead of a blocking Windows box."""
import ctypes
import os


def handle_native_errors():
    if os.name!='nt':return
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetErrorMode.restype=ctypes.c_uint
    kernel.SetErrorMode.argtypes=[ctypes.c_uint];kernel.SetErrorMode.restype=ctypes.c_uint
    # Critical-loader and crash error modes are inherited by children. They do not grant execution
    # permission or change Code Integrity policy; rejected modules still fail.
    kernel.SetErrorMode(kernel.GetErrorMode()|0x0001|0x0002)

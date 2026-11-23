"""Store TIDAL refresh tokens in the macOS Keychain."""
import ctypes as c
import sys

SERVICE = b"tidal-scripts.oauth"
NOT_FOUND = -25300


def refresh_token(account, value=None):
    if sys.platform != "darwin":
        return None

    security = c.CDLL("/System/Library/Frameworks/Security.framework/Security")
    core = c.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    pointer = c.c_void_p
    size = c.c_uint32
    keychain_args = [pointer, size, c.c_char_p, size, c.c_char_p]
    security.SecKeychainFindGenericPassword.argtypes = keychain_args + [
        c.POINTER(size), c.POINTER(pointer), c.POINTER(pointer)]
    security.SecKeychainAddGenericPassword.argtypes = keychain_args + [size, c.c_char_p, pointer]
    security.SecKeychainItemModifyAttributesAndData.argtypes = [pointer, pointer, size, c.c_char_p]
    security.SecKeychainItemFreeContent.argtypes = [pointer, pointer]
    core.CFRelease.argtypes = [pointer]
    core.CFRelease.restype = None

    account = account.encode()
    args = (None, len(SERVICE), SERVICE, len(account), account)
    length, data, item = size(), pointer(), pointer()
    status = security.SecKeychainFindGenericPassword(
        *args, c.byref(length) if value is None else None,
        c.byref(data) if value is None else None, c.byref(item))
    try:
        if value is None:
            if status == NOT_FOUND:
                return None
            if status == 0:
                return c.string_at(data, length.value).decode()
        else:
            secret = value.encode()
            if status == NOT_FOUND:
                status = security.SecKeychainAddGenericPassword(*args, len(secret), secret, None)
            elif status == 0:
                status = security.SecKeychainItemModifyAttributesAndData(item, None, len(secret), secret)
            if status == 0:
                return value
        raise RuntimeError(f"macOS Keychain returned error {status}.")
    finally:
        if data:
            security.SecKeychainItemFreeContent(None, data)
        if item:
            core.CFRelease(item)

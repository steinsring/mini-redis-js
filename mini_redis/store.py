from typing import cast

from .resp import RespError, RespSimple, RespValue


class KeyValueStore:
    """Binary keys and values; application key prefixes have no special meaning."""

    def __init__(self) -> None:
        self._values: dict[bytes, bytes] = {}

    async def execute(self, request: RespValue) -> RespValue:
        if not isinstance(request, list) or not request or not all(isinstance(v, bytes) for v in request):
            return RespError("ERR expected an array of bulk strings")
        command, *args = cast(list[bytes], request)
        command = command.upper()
        if command == b"GET" and len(args) == 1:
            return self._values.get(args[0])
        if command == b"SET" and len(args) == 2:
            self._values[args[0]] = args[1]
            return RespSimple("OK")
        if command == b"DEL" and args:
            return sum(self._values.pop(key, None) is not None for key in args)
        return RespError("ERR invalid arguments" if command in (b"GET", b"SET", b"DEL") else "ERR unsupported command")

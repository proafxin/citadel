from functools import lru_cache

from redis.commands.core import AsyncScript

from citadel.bus import get_redis

STREAM_LIBRARY_READY = "library_ready"
LIBRARY_FLAGS_TTL = 86_400

_RECORD_FLAG_LUA = """
redis.call('HSET', KEYS[1], ARGV[1], '1')
redis.call('EXPIRE', KEYS[1], ARGV[2])
if redis.call('HGET', KEYS[1], 'summaries') == '1' and redis.call('HGET', KEYS[1], 'embedding') == '1' then
  redis.call('XADD', KEYS[2], '*', 'library_id', ARGV[3])
  return 1
end
return 0
"""


@lru_cache
def _record_flag_script() -> AsyncScript:
    return get_redis().register_script(_RECORD_FLAG_LUA)


def flags_key(library_id: int) -> str:
    return f"ready_flags:{library_id}"


async def record_library_flag(library_id: int, flag: str) -> None:
    await _record_flag_script()(
        keys=[flags_key(library_id), STREAM_LIBRARY_READY], args=[flag, LIBRARY_FLAGS_TTL, str(library_id)]
    )

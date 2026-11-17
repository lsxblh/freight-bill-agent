"""重试：把"偶发故障"与"系统故障"区分开。"""

from __future__ import annotations

import functools
import logging
import random
import time
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


def retry(
    times: int = 3,
    *,
    base_delay: float = 0.8,
    max_delay: float = 8.0,
    jitter: float = 0.2,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    logger: logging.Logger | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """指数退避重试（带抖动），只重试指定异常。"""

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            last_error: BaseException | None = None
            for attempt in range(1, times + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_error = exc
                    if attempt >= times:
                        break
                    delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                    delay += random.uniform(0, jitter * delay)
                    if logger:
                        logger.warning(
                            "%s 第 %d/%d 次失败：%s，%.1fs 后重试",
                            func.__name__,
                            attempt,
                            times,
                            exc,
                            delay,
                        )
                    time.sleep(delay)
            raise last_error if last_error else RuntimeError("retry failed")

        return wrapper

    return decorator


__all__ = ["retry"]

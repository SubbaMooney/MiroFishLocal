"""
OASIS 平台可用动作（Twitter / Reddit）的唯一真实来源（Single Source of Truth）

设计要点：
- 此模块只依赖标准库；不在导入时依赖 oasis / camel-ai。
  原因：app.config 在 Flask 启动早期被加载，此时第三方 oasis 库可能还没初始化。
  Worker 脚本（backend/scripts/run_*_simulation.py）按需调用 get_*_action_types()，
  在那个时机 oasis 已经导入完毕。
- TWITTER_ACTION_NAMES / REDDIT_ACTION_NAMES 是字符串元组，供 Config 暴露给 API 层。
- get_twitter_action_types() / get_reddit_action_types() 返回 ActionType 枚举列表，
  Worker 脚本将其传给 oasis.make(available_actions=...)。
- 历史上 INTERVIEW 动作不放入可用列表（只能通过 ManualAction 手动触发）。
"""

from typing import List, Tuple


# 字符串名称（不依赖 oasis）—— 这是真正的 Source of Truth
TWITTER_ACTION_NAMES: Tuple[str, ...] = (
    'CREATE_POST',
    'LIKE_POST',
    'REPOST',
    'FOLLOW',
    'DO_NOTHING',
    'QUOTE_POST',
)

REDDIT_ACTION_NAMES: Tuple[str, ...] = (
    'LIKE_POST',
    'DISLIKE_POST',
    'CREATE_POST',
    'CREATE_COMMENT',
    'LIKE_COMMENT',
    'DISLIKE_COMMENT',
    'SEARCH_POSTS',
    'SEARCH_USER',
    'TREND',
    'REFRESH',
    'DO_NOTHING',
    'FOLLOW',
    'MUTE',
)


def _resolve_action_types(names: Tuple[str, ...]) -> List:
    """
    将动作名称解析为 oasis.ActionType 枚举对象列表

    懒加载导入 oasis，避免在 Flask 启动期触发重型依赖加载。
    若有任何名称无法解析为 ActionType，立即抛出 AttributeError，
    防止静默漂移（drift）。
    """
    from oasis import ActionType  # 懒加载

    resolved = []
    for name in names:
        action_type = getattr(ActionType, name, None)
        if action_type is None:
            raise AttributeError(
                f"oasis.ActionType 缺少枚举值 '{name}'，"
                f"请检查 backend/app/services/oasis_actions.py 与 oasis 库的版本一致性"
            )
        resolved.append(action_type)
    return resolved


def get_twitter_action_types() -> List:
    """返回 Twitter 平台可用动作的 ActionType 枚举列表"""
    return _resolve_action_types(TWITTER_ACTION_NAMES)


def get_reddit_action_types() -> List:
    """返回 Reddit 平台可用动作的 ActionType 枚举列表"""
    return _resolve_action_types(REDDIT_ACTION_NAMES)

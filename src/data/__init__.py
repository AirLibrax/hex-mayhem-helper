"""数据层：双数据源 Provider 抽象 + SQLite 缓存"""
from .base import StatsProvider, ProviderError
from .cache import StatsCache
from .aramgg import AramggProvider
from .hexdata import HexdataProvider

PROVIDERS = {
    "aramgg": AramggProvider,
    "hexdata": HexdataProvider,
}


def create_provider(name: str, api_key: str = "") -> StatsProvider:
    if name not in PROVIDERS:
        raise ProviderError(f"未知数据源: {name}（可选: {', '.join(PROVIDERS)}）")
    cls = PROVIDERS[name]
    try:
        return cls(api_key=api_key)
    except TypeError:
        return cls()

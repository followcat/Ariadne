"""Official optional plugins (odoo / gitlab / redmine).

Plugin model (revised NON_GOALS): plugins are optional, independently
configured modules. The host enables a plugin with its own url/token;
plugin tools register into the ONE capability registry at compose time.
Kernel core never depends on them.
"""

from .base import PLUGIN_REGISTRY, Plugin, build_plugin_tools
from .store import PluginStore

__all__ = ["PLUGIN_REGISTRY", "Plugin", "PluginStore", "build_plugin_tools"]

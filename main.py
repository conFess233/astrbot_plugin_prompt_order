"""提示词编排插件入口：注册钩子、组装服务、停用时清理。"""

from __future__ import annotations

from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .services import settings as settings_service
from .services.inspector import InspectorService
from .services.orderer import STATE_KEY, OrderService
from .services.web_api import WebApi

PLUGIN_NAME = "astrbot_plugin_prompt_order"
# 第一个执行：拍基线、补包装。其余插件的钩子都在它之后。
BEGIN_PRIORITY = 2_000_000_000
# 在所有注入提示词的插件之后、cost_control 尾部诊断（-100000）之前。
ARRANGE_PRIORITY = -99999


class PromptOrderPlugin(Star):
    """按用户布局重排提示词片段，并提供调用检查器。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.settings = settings_service.load(config)
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        data_dir.mkdir(parents=True, exist_ok=True)
        self.order = OrderService(
            data_dir, self.settings, own_module=type(self).__module__.rsplit(".", 1)[0]
        )
        self.inspector = InspectorService(data_dir, self.settings, context)
        self.order.inspector = self.inspector
        self.web = WebApi(context, config, self.order, self.inspector, self._reload_settings)
        self._started = False

    async def initialize(self) -> None:
        self.order.start()
        self.inspector.start()
        self.web.register()
        self._started = True
        mode = "试运行" if self.settings.dry_run else "生效中"
        inspector = "开启" if self.settings.inspector.enabled else "关闭"
        logger.info(f"提示词编排：已启动，重排{mode}，检查器{inspector}")

    def _reload_settings(self) -> None:
        self.settings = settings_service.load(self.config)
        self.order.settings = self.settings
        self.inspector.apply_settings(self.settings)
        self.inspector.refresh()

    @filter.on_llm_request(priority=BEGIN_PRIORITY)
    async def po_begin(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """拍下宿主生成的基线，并给本轮所有钩子补上来源追踪。"""

        if not self._started:
            return
        try:
            self.order.begin(event, req)
        except Exception:
            logger.warning("提示词编排：基线快照失败，本轮不追踪", exc_info=True)

    @filter.on_llm_request(priority=ARRANGE_PRIORITY)
    async def po_arrange(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """按布局重排；任何异常都放弃重排，请求原样发出。"""

        if not self._started:
            return
        try:
            self.order.arrange(event, req)
        except Exception:
            logger.warning("提示词编排：重排失败，本次请求保持原样", exc_info=True)

    @filter.on_agent_done()
    async def po_done(self, event: AstrMessageEvent, run_context, response) -> None:
        """本轮结束：补全样本里的后续改动，并结束检查器记录。"""

        state = event.get_extra(STATE_KEY)
        if state is None:
            return
        try:
            self.order.finish(event)
            self.inspector.close_main(state)
        except Exception:
            logger.warning("提示词编排：结束本轮记录失败", exc_info=True)

    async def terminate(self) -> None:
        self._started = False
        self.order.stop()
        await self.inspector.stop()
        logger.info("提示词编排：已停用，钩子与 provider 包装已还原")

"""全量余额查询：查询账户钱包，而不是 API 令牌限额。"""

import asyncio
from decimal import Decimal, InvalidOperation
from string import Formatter
from urllib.parse import urlsplit

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


DEFAULT_REPLY_TEMPLATE = "当前New API余额为：{newapi_balance}\ndeepseek余额为：{deepseek_balance}"


def render_reply(template, newapi_balance, deepseek_balance):
    """只允许两个具名字段；无效模板回退，防止遗漏查询结果。"""
    values = {"newapi_balance": newapi_balance, "deepseek_balance": deepseek_balance}
    if not isinstance(template, str) or not template.strip():
        return DEFAULT_REPLY_TEMPLATE.format_map(values)
    try:
        if len(template) > 4000:
            raise ValueError("template too long")
        fields = set()
        for _, field, spec, conversion in Formatter().parse(template):
            if field is not None:
                if field not in values or spec or conversion:
                    raise ValueError("unsupported placeholder")
                fields.add(field)
        if fields != set(values):
            raise ValueError("both balance placeholders are required")
        return template.format_map(values)
    except ValueError:
        logger.warning("全量余额查询：自定义回复模板无效，已使用默认模板")
        return DEFAULT_REPLY_TEMPLATE.format_map(values)


class BalanceError(Exception):
    """仅携带可向用户展示的固定错误，不包含服务端响应或密钥。"""


def number(value):
    if isinstance(value, bool) or value is None:
        raise BalanceError("接口金额格式不正确")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise BalanceError("接口金额格式不正确") from None
    if not result.is_finite() or abs(result) > Decimal("1e24"):
        raise BalanceError("接口金额超出有效范围")
    return result


def money(value):
    return format(number(value), ".4f")


def newapi_amount(quota, status):
    """跟随站点显示单位；缺少换算参数时拒绝猜测余额。"""
    quota = number(quota)
    kind = status.get("quota_display_type")
    if not kind:
        legacy = status.get("display_in_currency")
        kind = "USD" if legacy is True else "TOKENS" if legacy is False else None
    if kind == "TOKENS":
        return f"{quota:f} 额度单位"
    unit = number(status.get("quota_per_unit"))
    if unit <= 0:
        raise BalanceError("站点 quota_per_unit 必须大于零")
    amount = quota / unit
    if kind == "USD":
        return f"{money(amount)} USD"
    if kind == "CNY":
        rate = number(status.get("usd_exchange_rate"))
        if rate <= 0:
            raise BalanceError("站点汇率必须大于零")
        return f"{money(amount * rate)} CNY"
    if kind == "CUSTOM":
        rate = number(status.get("custom_currency_exchange_rate"))
        symbol = str(status.get("custom_currency_symbol", "")).strip()
        if rate <= 0 or not symbol or len(symbol) > 8 or any(c in symbol for c in "\r\n"):
            raise BalanceError("自定义币种配置无效")
        return f"{money(amount * rate)} {symbol}"
    raise BalanceError("无法识别站点余额单位，请检查站点设置")


def deepseek_amount(payload):
    infos = payload.get("balance_infos")
    if not isinstance(infos, list) or not infos:
        raise BalanceError("DeepSeek 未返回余额数据")
    results = []
    for info in infos:
        if not isinstance(info, dict) or info.get("currency") not in ("CNY", "USD"):
            raise BalanceError("DeepSeek 返回了未知币种")
        results.append(f"{money(info.get('total_balance'))} {info['currency']}")
    return " / ".join(results)


class AllBalancePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    def _timeout(self):
        try:
            return max(1, min(60, int(self.config.get("timeout_seconds", 15))))
        except (ValueError, TypeError):
            return 15

    @staticmethod
    async def _get(session, url, headers=None):
        try:
            async with session.get(url, headers=headers, allow_redirects=False) as response:
                if response.status in (401, 403):
                    raise BalanceError("鉴权失败，请检查凭据、用户 ID 和访问权限")
                if response.status == 429:
                    raise BalanceError("请求过于频繁，请稍后再试")
                if response.status != 200:
                    raise BalanceError(f"接口请求失败（HTTP {response.status}）")
                try:
                    payload = await response.json(content_type=None)
                except (ValueError, UnicodeError):
                    raise BalanceError("接口未返回有效 JSON，请检查地址或站点防护") from None
                if not isinstance(payload, dict):
                    raise BalanceError("接口响应结构不正确")
                return payload
        except asyncio.TimeoutError:
            raise BalanceError("查询超时，请稍后再试") from None
        except aiohttp.ClientError:
            raise BalanceError("网络连接失败，请检查网络或证书") from None

    async def _newapi(self, session):
        base = str(self.config.get("newapi_base_url", "https://new.jzeri.cn")).strip().rstrip("/")
        parsed = urlsplit(base)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise BalanceError("New API 地址必须为不含凭据和查询参数的 HTTPS 站点地址")
        token = str(self.config.get("newapi_access_token", "")).strip()
        user_id = str(self.config.get("newapi_user_id", "")).strip()
        if not token:
            raise BalanceError("未配置 New API 系统访问令牌（不是模型调用密钥）")
        headers = {"Authorization": f"Bearer {token}"}
        if user_id:
            if not user_id.isascii() or not user_id.isdigit() or int(user_id) <= 0:
                raise BalanceError("New API 用户 ID 必须为正整数或留空")
            headers["New-Api-User"] = user_id
        payload = await self._get(session, base + "/api/user/self", headers)
        if payload.get("success") is not True or not isinstance(payload.get("data"), dict):
            raise BalanceError("New API 查询失败，请检查系统访问令牌和用户 ID")
        quota = payload["data"].get("quota")
        status = await self._get(session, base + "/api/status")
        if status.get("success") is not True or not isinstance(status.get("data"), dict):
            raise BalanceError("无法读取站点额度换算设置")
        return newapi_amount(quota, status["data"])

    async def _deepseek(self, session):
        key = str(self.config.get("deepseek_api_key", "")).strip()
        if not key:
            raise BalanceError("未配置 DeepSeek 官方 API Key")
        payload = await self._get(session, "https://api.deepseek.com/user/balance", {"Authorization": f"Bearer {key}"})
        return deepseek_amount(payload)

    @staticmethod
    async def _safe_query(query):
        try:
            return await query, True
        except BalanceError as exc:
            return f"查询失败（{exc}）", False
        except Exception as exc:
            # 不记录异常正文，避免 SDK/HTTP 异常携带密钥。
            logger.warning("全量余额查询异常类型：%s", type(exc).__name__)
            return "查询失败（内部异常，请检查插件配置）", False

    async def _narrate(self, event, template, amounts):
        try:
            provider_id = str(self.config.get("llm_provider_id", "")).strip()
            if not provider_id:
                provider_id = await self.context.get_current_chat_provider_id(umo=event.unified_msg_origin)
            if not provider_id:
                return template
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                system_prompt="用简短自然的中文告知用户两项账户余额。必须保留New API和deepseek名称，以及每项金额、币种原文，不得交换归属，不要合并不同币种。不得编造、换算或推测数据。不要添加链接，不需要工具。",
                prompt="请自然地转述以下查询结果：\n" + template + "\n核实数据：New API：" + amounts[0] + "；deepseek：" + amounts[-1],
            )
            text = response.completion_text
            if not isinstance(text, str) or not text.strip() or len(text) > 1000:
                return template
            if not all(value in text for value in amounts):
                return template
            if "new api" not in text.lower() or "deepseek" not in text.lower():
                return template
            return text.strip()
        except Exception as exc:
            logger.warning("全量余额查询 LLM 回退模板，异常类型：%s", type(exc).__name__)
            return template

    @filter.command("余额")
    async def balance(self, event: AstrMessageEvent):
        """查询New API 和 DeepSeek 账户余额。"""
        if self.config.get("admin_only", True) and not event.is_admin():
            yield event.plain_result("仅 AstrBot 管理员可以查询余额。")
            event.stop_event()
            return
        timeout = aiohttp.ClientTimeout(total=self._timeout())
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            newapi, deepseek = await asyncio.gather(
                self._safe_query(self._newapi(session)),
                self._safe_query(self._deepseek(session)),
            )
        template = render_reply(self.config.get("reply_template", DEFAULT_REPLY_TEMPLATE), newapi[0], deepseek[0])
        text = template
        if self.config.get("enable_llm", False) and newapi[1] and deepseek[1]:
            try:
                text = await asyncio.wait_for(
                    self._narrate(event, template, [newapi[0], deepseek[0]]),
                    timeout=self._timeout(),
                )
            except asyncio.TimeoutError:
                text = template
        yield event.plain_result(text)
        event.stop_event()

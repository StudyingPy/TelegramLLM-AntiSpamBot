"""Simplified Chinese user-facing text for the fingerprint review WebUI.

API error/result messages and the display labels for internal codes live here so they
can be tested in one place. The static frontend receives the labels from /api/meta and
only falls back to its own copy when a code is unknown.
"""

from __future__ import annotations

ERRORS = {
    "unauthenticated": "登录已失效，请关闭后从 Telegram 重新打开复核台。",
    "init_data_invalid": "身份校验失败，请从 Telegram 中打开复核台。",
    "init_data_expired": "登录信息已过期，请关闭后从 Telegram 重新打开复核台。",
    "forbidden": "仅指定复核人可以使用指纹复核台。",
    "not_found": "请求的内容不存在。",
    "review_not_found": "复核记录不存在，可能指纹已被删除。",
    "fingerprint_not_found": "指纹不存在，可能已被删除。",
    "method_not_allowed": "不支持该请求方式。",
    "bad_request": "请求参数无效。",
    "bad_json": "请求内容格式无效。",
    "unsupported_media_type": "请求内容必须为 JSON 格式。",
    "payload_too_large": "请求内容过大。",
    "confirmation_required": "删除需要二次确认，请在确认对话框中点击“确认删除”。",
    "request_id_conflict": "该请求编号已用于其他操作，请刷新页面后重试。",
    "too_many_items": "一次最多可选择 {limit} 条。",
    "duplicate_items": "所选项目有重复，请刷新后重新选择。",
    "server_not_configured": "复核台尚未配置完成，请联系管理员。",
    "internal": "服务器内部错误，请稍后重试。",
}

# Field labels used in validation messages ("{label}无效。").
FIELD_LABELS = {
    "status": "复核状态",
    "q": "搜索内容",
    "type": "指纹类型",
    "source": "来源",
    "enabled": "启用状态",
    "weight_min": "最低权重",
    "weight_max": "最高权重",
    "hits_min": "最少命中次数",
    "hits_max": "最多命中次数",
    "fp_min": "最少误判次数",
    "fp_max": "最多误判次数",
    "sort": "排序方式",
    "offset": "分页位置",
    "limit": "每页数量",
    "request_id": "请求编号",
    "action": "操作类型",
    "items": "所选项目",
    "review_id": "复核编号",
    "version": "版本标记",
    "init_data": "登录信息",
    "id": "编号",
}

ITEM_RESULTS = {
    "ok": {
        "disable": "已停用，该指纹将停止匹配。",
        "keep": "已保留，该指纹继续生效。",
        "restore": "已恢复，该指纹重新开始匹配。",
        "delete": "已删除。",
    },
    "already_done": {
        "disable": "此前已停用，无需重复操作。",
        "keep": "此前已保留，无需重复操作。",
        "restore": "此前已恢复，无需重复操作。",
        "delete": "该指纹此前已删除。",
    },
    "stale": "该指纹的状态或证据已变化，已重新加载最新内容，请确认后再操作。",
    "not_found": "复核记录或指纹不存在，可能已被删除。",
    "invalid": "当前状态不支持此操作，请刷新后重试。",
}

LABELS = {
    "fingerprint_types": {
        "phrase": "短语",
        "skeleton": "句式模板",
        "content": "整句内容",
    },
    "sources": {
        "llm_spam": "模型判定广告",
        "llm_spam_phrase": "模型提取短语",
        "vote_confirmed": "投票确认广告",
        "manual": "手动添加",
        "disabled_false_positive": "多次误判降级",
    },
    "evidence_labels": {
        "manual_ham": "人工放行",
        "manual_spam": "人工确认广告",
        "llm_benign": "模型判正常",
        "llm_spam": "模型判广告",
        "local_hit": "本地规则命中",
    },
    "review_statuses": {
        "pending": "待复核",
        "kept": "已保留",
        "disabled": "已停用",
        "restored": "已恢复",
        "not_recommended": "未建议停用",
    },
    "decisions": {
        "disable": "建议停用",
        "keep": "建议保留",
        "insufficient_evidence": "证据不足",
    },
    "actions": {
        "disable": "停用",
        "keep": "保留",
        "restore": "恢复",
        "delete": "删除",
    },
}


def error_message(code: str, **kwargs: object) -> str:
    template = ERRORS.get(code, ERRORS["internal"])
    return template.format(**kwargs) if kwargs else template


def field_error(field: str) -> str:
    return f"{FIELD_LABELS.get(field, '请求参数')}无效。"


def item_message(status: str, action: str) -> str:
    message = ITEM_RESULTS.get(status, ITEM_RESULTS["invalid"])
    if isinstance(message, dict):
        return message.get(action, ITEM_RESULTS["invalid"])
    return message

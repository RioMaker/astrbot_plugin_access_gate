# AstrBot 独立白名单门禁

这个插件用于替代 AstrBot 核心的统一 ID 白名单，将群聊和私聊拆成两套互不影响的白名单，并默认让私聊 /rtlink 指令绕过私聊白名单。

## 必须先做的设置

在 AstrBot WebUI 中关闭核心的 ID 白名单开关 enable_id_white_list，然后重载或重启 AstrBot。

如果核心白名单仍开启，消息会在进入插件处理器之前被 AstrBot 拦截，本插件无法让 /rtlink 绕过它。

## 默认行为

- 群聊白名单和私聊白名单都默认开启。
- 空的群聊白名单会拒绝所有普通群聊。
- 空的私聊白名单会拒绝所有普通私聊。
- AstrBot 管理员默认可以绕过群聊和私聊白名单。
- 私聊 /rtlink、/rtlink bind、/rtlink score 等同一根指令下的用法默认放行。
- /rtlinkevil 不会被误判为 /rtlink。
- 被拒绝的消息不回复，并阻止后续插件和 LLM 继续处理。
- WebChat 默认放行，便于在 WebUI 中调试。

## 配置方法

1. 在 AstrBot 插件管理中加载 astrbot_plugin_access_gate。
2. 打开插件配置。
3. 在“群聊白名单”中逐行填写允许的群号。
4. 在“私聊白名单”中逐行填写允许的用户 ID；QQ 场景直接填写 QQ 号。
5. 保留“私聊白名单豁免指令”中的 rtlink，即可让所有用户私聊使用 /rtlink。

## 管理员批量导入

管理员可以直接发送以下指令，将内容合并进现有名单并自动去重：

    /accessgate import group <JSON、逐行或逗号分隔的群号>
    /accessgate import private <JSON、逐行或逗号分隔的用户 ID>

也支持中文写法：

    /accessgate 导入 群聊 <内容>
    /accessgate 导入 私聊 <内容>

可以直接粘贴完整 JSON、JSON 数组，或类似下面这种单个字段片段：

    /accessgate import group "id_whitelist": ["123456789", "987654321"],

该管理指令会优先于门禁执行，但只有 AstrBot 管理员能够导入；非管理员调用时不会收到回复。发送 /accessgate help 可查看用法。

白名单也接受完整的 unified_msg_origin。如果数字 ID 与适配器实际标识不一致，可以从 AstrBot 日志中的门禁拦截记录复制 umo 值加入名单。日志不会记录消息正文。

## 安全提醒

关闭核心白名单后，本插件就是新的访问边界。不要在核心白名单关闭期间禁用或卸载本插件，否则消息将不再受这套名单过滤。

如果需要临时开放某一类会话，请关闭对应的“启用群聊白名单”或“启用私聊白名单”，不要同时停用整个插件。

# 🎯 社交管理插件 (astrbot_plugin_social_manager)

一个面向 QQ 群聊的 **多功能 AstrBot 插件**，集成**群管理、点歌/查歌词/歌单、好感度(8维情感)、银行系统、自我觉醒、唤醒增强、帮助指令、WebUI管理面板**于一体。

---

## 📦 功能一览

| 模块 | 指令 | 说明 |
|------|------|------|
| **👥 群管理** | `/群管 禁言 <QQ> <秒>` `/群管 解禁 <QQ>` `/群管 踢出 <QQ>` `/群管 全体禁言` `/群管 取消全体禁言` `/群管 名片 <QQ> <名片>` | 基于 OneBot API，需要管理员权限 |
| **💬 QQ空间** | `/发说说 <内容>` | 发布 QQ 说说（需管理员） |
| **🎵 点歌** | `/点歌 <关键词>`（自动选歌）/ 或 `网易点歌` `QQ点歌` `酷狗点歌` 等 12 种平台 | 支持卡片、语音、文件、文本四种发送模式 |
| **📝 查歌词** | `/查歌词 <歌名>` | 搜索并返回歌词 |
| **📋 歌单** | `/歌单收藏 <歌名>` `/歌单取藏 <歌名>` `/歌单列表` `/歌单点歌 <序号>` | 个人歌单管理 |
| **❤️ 高级好感度** | `/好感度 查询 [QQ]` `/好感度 详情 [QQ]` `/好感度 赠送 <QQ>` `/好感度 排行 [数量]` | 8维情感模型 + 关系阶段演化 |
| **🏦 银行系统** | `/银行 开户` `/银行 余额` `/银行 转账 <QQ> <金额>` `/银行 排行` | 虚拟金币经济 |
| **📅 签到** | `/签到` | 每日金币 + 好感度 |
| **🤖 自我觉醒** | `/开启觉醒` `/关闭觉醒` | 机器人定时在群内主动发言 |
| **🔊 唤醒增强** | `/wbegin` `/wexit` `/wgid` + 正则匹配唤醒 + 持续对话 | 无需指令前缀即可对话 |
| **ℹ️ 帮助** | `/helps` `/帮助` `/菜单` `/功能` | 生成带图片的命令帮助 |
| **📊 WebUI面板** | http://0.0.0.0:1111 | 独立 WebUI 可视化管理好感/金币/排行 |
| **🔌 插件Pages** | AstrBot WebUI 插件详情页 | 内嵌仪表盘 |
| **🧠 LLM集成** | 自动注入情感上下文、自动更新好感度 | 让 AI 回复更贴近关系状态 |

---

## 🚀 安装

### 方法一：AstrBot WebUI 插件市场
搜索 `astrbot_plugin_social_manager` 一键安装。

### 方法二：手动安装
```bash
cd AstrBot/data/plugins
git clone https://github.com/AwdndWindowsServer1145/astrbot_plugin_social_manager.git
pip install -r astrbot_plugin_social_manager/requirements.txt
```

然后在 WebUI 插件管理中 **重载插件**。

---

## ⚙️ 配置

在 AstrBot WebUI 的插件配置页面可视化编辑以下参数：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `admin_qq` | `""` | 管理员 QQ（多账号用逗号分隔） |
| `enable_webui` | `true` | 启用 WebUI 面板 |
| `webui_port` | `1111` | WebUI 端口 |
| `default_favorability` | `50` | 新用户默认好感度 |
| `max_favorability` | `100` | 好感度上限 |
| `bank_initial_balance` | `100` | 开户赠送金币 |
| `daily_signin_reward` | `20` | 每日签到金币 |
| `self_awake_interval` | `120` | 觉醒发言间隔(0=关闭) |
| `waking_regex` | `[]` | 唤醒正则表达式列表 |
| `default_player_name` | `"网易点歌"` | 默认点歌平台 |
| `send_modes` | `[card,record,file,text]` | 发送模式优先级 |

完整配置项参见 `_conf_schema.json`（36项）。

---

## 📁 文件结构

```
astrbot_plugin_social_manager/
├── main.py                 # 主插件 (1185行)
├── metadata.yaml           # 元数据
├── _conf_schema.json       # 可视化配置
├── requirements.txt        # 依赖
├── help_draw.py            # 帮助图片渲染
├── core/                   # 音乐引擎核心
│   ├── config.py
│   ├── downloader.py
│   ├── platform/           # 多音源支持
│   │   ├── ncm.py          # 网易云
│   │   ├── ncm_nodejs.py   # 网易云(nodejs)
│   │   ├── txqq.py         # QQ音乐
│   │   ├── searcher.py     # 聚合搜索
│   │   └── base.py         # 基类
│   ├── playlist.py         # 歌单管理
│   ├── renderer.py         # 渲染器
│   └── sender.py           # 发送器
├── fonts/                  # 字体资源
├── pages/dashboard/        # AstrBot WebUI Pages 仪表盘
└── webui/                  # 独立 WebUI (端口1111)
```

---

## 💾 数据存储

所有数据持久化在 `data/plugin_data/astrbot_plugin_social_manager/`：

| 文件 | 内容 |
|------|------|
| `favorability.json` | 简单好感度 |
| `emotion.json` | 8维情感 + 关系阶段 + 态度 |
| `bank.json` | 银行金币 |
| `signin.json` | 签到记录 |

---

## 🧠 情感系统

采用 **8维情感模型**（喜悦、信任、恐惧、惊讶、悲伤、厌恶、愤怒、期待），根据对话内容自动演化：

- **关系阶段**：敌对期 → 疏远期 → 初识期 → 深化期 → 亲密期 → 共生期
- **态度**：敌对 → 冷淡 → 中立 → 友好 → 热情 → 亲密
- 自动通过 `on_llm_request` 注入上下文，`on_llm_response` 更新状态

---

## 🖥️ WebUI 管理面板

访问 `http://你的机器人IP:1111` 可查看：
- 系统状态概览
- 用户好感/金币排行
- 用户详情查询
- 好感/金币管理操作

---

## 🔧 开发

```bash
git clone https://github.com/AwdndWindowsServer1145/astrbot_plugin_social_manager.git
cd astrbot_plugin_social_manager
# 修改代码后，在 AstrBot WebUI 重载插件
```

---

## 📄 许可证

MIT License

## 🙏 致谢

- [Soulter/astrbot_plugin_wake_enhance](https://github.com/Soulter/astrbot_plugin_wake_enhance) - 唤醒增强灵感
- [tinkerbellqwq/astrbot_plugin_help](https://github.com/tinkerbellqwq/astrbot_plugin_help) - 帮助指令参考
- [asakiyoshi/EmotionAI-Pro](https://github.com/asakiyoshi/EmotionAI-Pro) - 情感系统参考
- [Zhalslar/astrbot_plugin_music](https://github.com/Zhalslar/astrbot_plugin_music) - 音乐引擎

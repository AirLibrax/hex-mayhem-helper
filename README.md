# 海克斯大乱斗助手（Hex Mayhem Helper）

LOL 极地大乱斗（ARAM）辅助悬浮窗：选人/载入阶段显示英雄胜率，对局中自动识别海克斯符文弹窗并给出**当前英雄的符文适配胜率**与装备推荐。

> 本项目与 Riot Games 无关，非官方出品。Riot Games 及其相关商标归 Riot Games, Inc. 所有。

## 功能

| 阶段 | 悬浮窗显示 |
|---|---|
| 选人 | 我方英雄胜率（含 ARAM 公共台可替换英雄），换人/重随自动刷新 |
| 载入画面 | 我方 + 敌方全部 10 名英雄胜率 |
| 对局中 | 装备推荐（常驻）+ 符文弹窗自动识别（仅弹窗出现时显示） |

- **符文识别**：截屏检测三选一弹窗 → Windows 自带 OCR 读卡片标题 → 匹配符文库 → 显示 `[T1] 名字 胜率`（T 级 = 官方推荐 rank，T1 金 / T2 紫 / T3 蓝）
- **英雄适配**：通过 LCU 只读接口识别当前英雄 → 交叉显示**该英雄使用此符文的胜率**（非全局胜率）
- **多屏支持**：设置内指定游戏所在屏幕，逐屏检测
- **数据源**：aramgg.com 官方 API（主）+ hexdata.com.cn（备用），本地缓存 24h 刷新
- **调试通道**：设置内「手动识别英雄」→「手动识别符文」，可完整验证识别通路

## 运行环境

- Windows 10 / 11（64 位）——OCR 依赖系统自带引擎
- Python 3.11+（源码运行）

## 快速开始（源码）

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
```

## 打包分发（免安装）

```bash
.venv\Scripts\pip install -r requirements-build.txt
python build.py
```

产物：`dist/HexMayhemHelper_v<版本>.zip`——解压后双击 `HexMayhemHelper.exe` 即用，无需安装任何依赖。

## API Key 配置

数据源使用 aramgg（data.dtodo.cn）API，免费额度 200 credits/天（本地缓存可大幅节省）。

- **默认**：`src/secrets.py` 内置共享 Key（已 .gitignore，不上传）
- **公开部署**：设置环境变量 `HEX_MAYHEM_API_KEY`，或参考 `src/secrets.example.py` 自行配置

## 合规说明

- 数据获取仅走 **LCU 只读接口**（lockfile 认证的本地 API）+ **系统级屏幕截图**
- 不使用游戏内存、无注入、无模拟输入、无流量劫持
- 展示的信息均为玩家客户端内可见内容与公开统计数据
- 依据 Riot 第三方应用政策：面向玩家的产品应在 [Riot Developer Portal](https://developer.riotgames.com/) 注册，使用 LCU API 的产品发布前需联系 Riot 告知

## 目录结构

```
├── main.py                 # 程序入口
├── src/
│   ├── config.py           # 配置（窗口/数据源/检测屏）
│   ├── secrets.py          # API Key（本地，不提交）
│   ├── secrets.example.py  # API Key 配置示例
│   ├── version.py          # 版本号
│   ├── paths.py            # 资源路径（源码/打包兼容）
│   ├── capture/            # 截图 + 弹窗检测 + OCR
│   ├── data/               # aramgg/hexdata 数据源 + SQLite 缓存
│   ├── lcu/                # LCU 只读接口
│   └── ui/                 # 悬浮窗 + 设置对话框
├── requirements.txt        # 运行时依赖
├── requirements-build.txt  # 构建依赖
├── build.py                # 一键打包脚本
└── README.md
```

## License

仅供学习交流使用。

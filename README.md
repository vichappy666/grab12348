# 12348 抢班次工具

广东 12348 公共法律服务平台（省热线平台法服岗）值班**抢班次**脚本。
给自己抢值班班次用：到点自动盯放班、发现名额立刻抢、抢到后桌面通知 + 语音播报。

> 上次没抢到的原因，是发请求那一刻名额已经满了（接口返回 `抢班人数已超过限制！`）。
> 本工具把「盯名额 → 抢」的循环压到几十毫秒级，并支持定时卡点、并发抢多个候选。

## 工作原理

抢班只依赖两个接口：

- `GET  .../getGrabSchedulingList/{日期}` —— 查某天可抢的班次（含名额 `已抢/总数`）
- `POST .../grabScheduling/{schedulingId}` —— 抢指定班次（靠 token 鉴权，无 body）

登录那一步需要手机短信验证码（没法自动化），所以采用 **token 模式**：
你手动登录一次，把 token 复制进配置，脚本拿 token 干活。
（token 里没有过期时间，是服务端 session 引用，登录后一段时间内一直有效。）

## 安装

需要 Python 3.9+。

```bash
cd grab12348
python3 -m venv .venv && source .venv/bin/activate   # 可选，推荐
pip install -r requirements.txt
```

## 怎么拿 token

1. 用手机或电脑浏览器登录 `https://gd.12348.gov.cn/cloudexamh5/login`
2. 打开开发者工具（手机可用电脑 Chrome 的「检查」/ Safari 远程调试；电脑直接 F12）
3. 切到 **Network（网络）** 面板，在页面上随便点一下排班相关操作，
   找到任意一个发往 `cloudh5api/...` 的请求
4. 看它的请求头 **Authorization**，值形如 `Bearer eyJhbGci...`，
   把 `Bearer ` 后面那一长串复制下来
5. 粘贴到 `config.yaml` 的 `auth.token`

> 提示：返回里带 `"token": "..."` 的登录响应里那串也是同一个 token。

## 配置

把 `config.example.yaml` 复制成 `config.yaml`（已自带一份），按注释填：

- `target.dates`：要抢哪天，如 `2026-05-22`
- `target.shift_names`：想要的班次，最想要的放最前；留空=有名额就抢任意一个
- `grab.start_at`：放班开抢时刻，如 `2026-05-20 00:00:00`；留空=运行即开抢
- `grab.aggressive`：秒光场景设 `true`（到点直接对已知班次盲抢，不等列表确认名额）

## 使用

```bash
# 1) 先校验 token 是否有效，看本月已排班 / 已满日期
python main.py check

# 2) 看某天有哪些可抢班次、各自还剩多少名额、对应 schedulingId
python main.py list 2026-05-22

# 3) 演练（不会真的抢）：确认筛选条件能命中你要的班
python main.py run --dry-run

# 4) 正式跑（按 config 里的 start_at 定时卡点）
python main.py run

# 立即开抢、忽略定时：
python main.py run --now
```

抢到后会：终端响铃、弹 macOS 桌面通知、用 `say` 播报「抢到班次了」。

## 实战建议

- **先 `check` 再 `list`**：确认 token 没过期、确认目标班次名和 schedulingId 对得上。
- **start_at 要对准放班时刻**：差几秒就可能错过。可提前在另一台机器 `date` 对一下时间。
- **名额秒光就开 `aggressive`**：标准模式要先看到「有名额」再抢，会慢半拍；
  盲抢模式到点直接发，更适合一放出就被抢光的热门班。
- **poll_interval 别调太狠**：这是政府服务器，你只是抢自己一个班，
  0.2~0.5 秒一轮足够，没必要每秒几十发。
- **token 会随重新登录而变**：失效了 `check` 会直接告诉你，重新复制即可。

## 目录结构

```
grab12348/
├── main.py                 # 命令行入口（check / list / run）
├── config.yaml             # 你的实际配置（含 token，已 gitignore）
├── config.example.yaml     # 配置模板
├── requirements.txt
└── grabber/
    ├── client.py           # 接口客户端（Session 复用 + keep-alive）
    ├── targeting.py        # 目标筛选与优先级排序
    ├── grabber.py          # 核心抢班引擎（探查 / 定时 / 并发抢）
    ├── scheduler.py        # 定时等待 + 桌面通知
    └── config.py           # 配置加载
```

## 说明

仅用于给本人抢取自己的值班班次，请遵守平台规则、合理设置频率。

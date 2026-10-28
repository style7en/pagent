# pagent

一个零依赖的命令行编程 Agent，单文件 1000 余行，只依赖 Python 标准库。
对接任意 OpenAI 兼容的 `/chat/completions` 接口，能读写文件、执行命令。

```
> 帮我把 utils.py 里的重复函数合并一下
→ read_file {"path": "utils.py"}
→ edit_file {"path": "utils.py", "old_string": "def _parse_a(x):", "new_string": "def _parse(x):"}
...
改完了：合并了 3 个重复的 _parse 函数，删掉了 42 行冗余代码。
```

## 要求

- Python 3.8+（无第三方依赖）
- 一个 OpenAI 兼容的接口地址和 API Key

## 快速开始

```bash
# 1. 配置：在同目录建 pagent.local.py（已在 .gitignore 中，密钥不会进仓库）
cat > pagent.local.py <<'EOF'
BASE_URL = "https://api.openai.com/v1"
API_KEY = "sk-xxxx"
MODEL = "gpt-4o-mini"
EOF

# 2. 交互模式
python pagent.py

# 3. 单次执行
echo "这个项目是做什么的？" | python pagent.py
```

配置优先级：**命令行参数 > 环境变量 `PAGENT_*` > `pagent.local.py` > 代码内 `DEFAULT_*`**。

### ⚠️ 不要把密钥写进 pagent.py

`pagent.py` 顶部的 `DEFAULT_API_KEY` / `DEFAULT_MODEL` / `DEFAULT_BASE_URL` 在仓库里
**必须保持空串**。一旦填了真实密钥并提交，就会随公开仓库泄漏出去（删掉文件也没用，
密钥仍留在 git 历史里）。

仓库自带的 `.git/hooks/pre-commit` 会在提交时拦截这种情况，但它只在本机生效
（`.git/hooks` 不随仓库分发），换机器克隆后需要重新放置。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PAGENT_API_KEY` | 空 | 接口密钥 |
| `PAGENT_MODEL` | 空 | 模型名 |
| `PAGENT_BASE_URL` | 空 | 接口地址，如 `https://api.openai.com/v1` |
| `PAGENT_WORKDIR` | 当前目录 | 工作目录（文件工具的沙箱根） |
| `PAGENT_ALLOW_OUTSIDE` | 关 | 设为 `1` 放开工作目录限制 |
| `PAGENT_CONTEXT_TOKENS` | `262144` | 上下文预算 |
| `PAGENT_MAX_OUTPUT_TOKENS` | `32768` | 单次最大输出 |
| `PAGENT_TIMEOUT` | `300` | 单次请求超时（秒） |
| `PAGENT_RETRIES` | `3` | 失败重试上限 |
| `PAGENT_RETRIES_429` | `8` | 429 限流单独的重试上限 |

## 命令行参数

```
--base-url URL      覆盖接口地址
--model NAME        覆盖模型名
--context N         上下文 token 预算
--max-output N      单次最大输出 token
--system TEXT       自定义系统提示词
--workdir DIR       工作目录（沙箱根）
--allow-outside     放开工作目录限制
-y, --yes           全部自动批准（跳过所有确认，危险）
--no-tools          禁用工具调用，纯聊天
--stream / --no-stream   强制开/关流式输出
--version           版本号
```

## 交互命令

| 命令 | 作用 |
|---|---|
| `/status` | 查看当前配置、token 用量与上下文占用百分比 |
| `/reset` | 清空对话历史 |
| `/tools` | 列出可用工具 |
| `/exit` | 退出（`/quit`、`/q` 同义） |
| `Ctrl+C` ×3 | 连按三次退出；只按一次是取消当前任务 |

## 工具

| 工具 | 说明 |
|---|---|
| `shell` | 执行 shell 命令，返回 stdout/stderr/退出码；超时会杀掉整个进程树 |
| `read_file` | 分片读文件，每次最多 8KB，输出带行号前缀，返回续读 offset |
| `edit_file` | 精确替换：`old_string` → `new_string`，要求原文唯一，否则报错不写盘 |
| `write_file` | 写文件，支持覆盖/追加，父目录自动创建 |
| `list_dir` | 列目录，目录在前 |

`read_file` 的输出形如：

```
   44→ CHUNK = 8192             # read_file 分片字节数
   45→ QUIT_AT = 3              # 连按几次 Ctrl+C 才退出
```

行号只为定位，**不属于文件内容**，喂给 `edit_file` 的 `old_string` 不要带行号。
改已有文件优先用 `edit_file`，只替换需要改的那一小段，比整文件重写省几十倍 token，
也不会误伤文件其它部分。新建文件或整篇重写才用 `write_file`。

## 安全策略

**普通命令直接执行，只有高危命令才请求确认。** 判定为高危的情况：

- 删除/权限/系统类命令：`rm`、`del`、`rmdir`、`chmod`、`sudo`、`reg`、`systemctl`、
  `shutdown`、`dd`、`mkfs`、`format`、`kill` 等，以及 PowerShell 的 `Remove-Item`、`Stop-Process`
- 改写历史：`git push --force`、`git reset --hard`、`git clean -f`、`git restore`
- 管道执行：`curl ... | sh`、`wget ... | bash`、`bash -c "..."` 里的内容
- 越界写入：`>` / `>>` 重定向到工作目录之外，或 `mv` 目标已存在/越界

文件工具的路径被限制在工作目录内（`--allow-outside` 可放开）。

> 注意：`shell` 本身不受路径沙箱约束，沙箱只管四个文件工具。高危识别是静态规则，
> `python -c "import shutil; shutil.rmtree('.')"` 这类藏在解释器里的调用判断不出来。
> 需要强隔离请用容器或降权运行。

## 其它机制

- **上下文裁剪**：超出预算时按「整轮丢弃 → 压缩工具结果 → 丢弃最旧工具往返块」三级处理，
  始终保证 `assistant/tool` 消息配对完整。
- **重试**：指数退避 + ±40% 随机抖动（1、2、4、8、16、32、60 秒封顶），
  优先采纳服务端返回的 `Retry-After`。429 默认给到 8 次机会。
- **编码**：子进程输出按 utf-8 → 系统编码 → gbk 依次尝试解码，中文 Windows 不乱码。
- **用量统计**：累计每次请求的输入/输出 token（服务端返回 `usage` 就用真实值，
  流式响应没带时按字符数估算并标注），`/status` 里查看。
- **上下文占用**：`/status` 显示当前对话占窗口的百分比、裁剪阈值，
  并把每次请求固定携带的 tools schema 也算进去，数字和真实请求对得上。
  超过阈值时会标「已触发裁剪」，此时可 `/reset` 重来。
- **emoji / 代理项**：服务端把 emoji 写成 `\ud83d\ude00` 这种转义时，JSON 解析出来是
  **两个孤立代理项**而不是一个字符，直接回传会抛
  `UnicodeEncodeError: surrogates not allowed`。收发两侧都会合并代理对、丢弃孤立项，
  请求体编码再加一层 `errors="replace"` 兜底，这条路不会再崩。

## 已知限制

- 只支持 `/chat/completions`，不兼容 `/responses` 接口
- 使用 `max_tokens` 字段，o1 / gpt-5 系等要求 `max_completion_tokens` 的模型需要自行调整
- 推理模型的 `reasoning_content` 不参与多轮回传

## License

[MIT](LICENSE) © 2026 style7en

可自由使用、修改、分发、商用，保留版权声明即可。完整条款见 [LICENSE](LICENSE)。

# 依赖与版本兼容

实际安装清单以 [`requirements.txt`](requirements.txt) 为准；本文件说明兼容性和升级方式，不是 pip 输入文件。

## Beancount 与查询模块

- 支持 Beancount v2 和 v3；版本范围见安装清单。
- v2 使用内置的 `beancount.query`，v3 使用独立的 [`beanquery`](https://github.com/beancount/beanquery)。只安装 Beancount v3 不足以运行机器人的查询功能。
- 两个查询接口的列描述格式不同，机器人已兼容。完整账本检查仍由当前环境安装的 Beancount 执行，并保留提交前检查和独立 LLM 审核。
- v3 支持单字母商品代码（例如 `O`），v2 解析规则不同。机器人部分输入校验仍采用较保守的规则，升级依赖不代表开放全部 v3 新语法；跨版本使用可继续采用 `O.US` 等标识，不要仅为启动机器人而修改历史账本。

## 安装或更新

在项目目录使用启动机器人的同一个 Python 安装依赖：

```bash
python3 -m pip install -r requirements.txt
python3 -c "from beancounter.bot import Bot; print('Import OK')"
```

导入检查不会启动轮询，也不会读取真实配置。若使用虚拟环境，将上述 `python3` 换成该环境的解释器，例如 `.venv/bin/python`；不要混用不同 Python 对应的 `pip`。

## 生产升级

先按 README 备份配置、个人 prompt 和状态文件，并检查工作区是否有本地修改。更新代码后安装依赖，再执行以下离线测试，最后在原有 tmux 会话中启动唯一一个机器人实例：

```bash
git pull --ff-only
python3 -m pip install -r requirements.txt
python3 tests/test_refactor.py
python3 tests/test_bot.py
python3 tests/test_fuzz.py
python3 tests/test_runtime.py
python3 main.py
```

上述升级应在维护窗口、旧机器人已正常停止后执行；不要同时运行两个实例。不要删除 `data/`，也不要在生产升级期间运行需要 `--live` 的交互式预览工具。首次从仍跟踪 `user.md` 的旧版本升级时，还需遵循 README 中的 prompt 备份与恢复步骤。

## 常见错误

- `No module named 'beancount.query'`：通常是旧代码运行在 v3 环境。更新代码并安装依赖；仅安装 `beanquery` 不能修复旧代码的导入路径。
- `No module named 'beanquery'`：在启动机器人使用的 Python 环境中重新安装 `requirements.txt`。
- `too many values to unpack (expected 2)` 出现在查询结果展示：旧格式化代码不兼容 beanquery 的列描述，需更新代码。

CI 对 Python 3.10/3.12 与 Beancount v2/v3 的组合运行全部自动测试。依赖升级通过测试并不代表生产账本的每个自定义插件都已验证；插件兼容性仍需结合实际账本检查。

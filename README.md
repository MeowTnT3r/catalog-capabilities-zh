# catalog-capabilities-zh

一个面向 Codex 的公开 skill：编排当前 Agent 已有的可信安装器，并为 skills、插件和市场能力维护一份有来源依据的中文说明目录。

它不会自己重写下载或安装逻辑。明确要求安装时，它会调用当前环境已有的 `skill-installer` 或插件管理能力；目标已经安装时，只更新说明；只有链接而没有安装意图时，只做检查和介绍。

## 安装

```bash
npx skills@latest add https://github.com/MeowTnT3r/catalog-capabilities-zh --skill catalog-capabilities-zh
```

也可以把 `skills/catalog-capabilities-zh` 复制到当前 Agent 的 skills 根目录。首次调用：

```text
$catalog-capabilities-zh
```

技能会列出检测到的 skills、插件和市场路径。确认扫描范围以及中文手册位置后，它才会保存本机 profile 并建立首次基线。

生成的手册顶部包含按功能分类的快速索引，每个条目可以跳转到完整说明。技能较多时可以直接搜索中文名、原始名、标签、别名、使用范围或触发词。

## 筛选与查找技能

除了维护中文说明目录，这个 skill 也可以帮助用户从已经收录的能力中筛选合适的 skill、插件或市场能力。可以直接使用自然语言描述需求，例如：

```text
$catalog-capabilities-zh 帮我筛选前端技能
$catalog-capabilities-zh 帮我找适合前端重构和 UI 设计的技能
$catalog-capabilities-zh 有哪些能力可以生成图片或处理 PDF？
$catalog-capabilities-zh 从现有技能里推荐几个适合调试复杂 Bug 的，并说明区别
```

筛选时会综合中文名称、原始标识、功能分类、标签、别名、使用范围、能力效果和典型触发词，并返回匹配项的安装状态、适用场景和主要限制。筛选和推荐本身是只读操作；只有用户同时明确要求安装某个结果时，才会进入安装编排流程。

## 四种工作模式

| 场景 | 行为 |
|---|---|
| 目标已安装 | 只核验并撰写中文说明 |
| 明确要求安装并提供目标 | 编排可信安装器，验证后写说明 |
| 只给链接、没有安装意图 | 只检查仓库和介绍能力 |
| 手动调用但没有目标 | 扫描现有能力并更新目录 |

多 skill 仓库会先展示候选清单并要求选择，不会默认全部安装。已经存在的 skill 不会被自动覆盖；检测到远程差异时只会提示更新。

## 本机文件

仓库中不包含任何个人路径。首次配置会在系统配置目录保存 `config.json` 和 `state.<profile>.json`：

- Windows：`%APPDATA%\catalog-capabilities-zh\`
- macOS：`~/Library/Application Support/catalog-capabilities-zh/`
- Linux：`${XDG_CONFIG_HOME:-~/.config}/catalog-capabilities-zh/`

设置 `CAPABILITY_CATALOG_CONFIG` 可以覆盖配置文件位置。profile 只记录扫描根目录、目录文档位置和选项，不保存账号或令牌。

## CLI

脚本仅依赖 Python 3.11+ 标准库：

```bash
python skills/catalog-capabilities-zh/scripts/catalog_capabilities.py discover --format json
python skills/catalog-capabilities-zh/scripts/catalog_capabilities.py configure --profile default --skill-root ~/.codex/skills --catalog ~/.codex/CAPABILITIES.zh-CN.md
python skills/catalog-capabilities-zh/scripts/catalog_capabilities.py scan --format json
python skills/catalog-capabilities-zh/scripts/catalog_capabilities.py diff --format json
python skills/catalog-capabilities-zh/scripts/catalog_capabilities.py render --summaries summaries.json --discoveries discoveries.json --format json
python skills/catalog-capabilities-zh/scripts/catalog_capabilities.py status --format json
python skills/catalog-capabilities-zh/scripts/catalog_capabilities.py search "前端 原型" --format text
```

`inspect-link` 只读取公开 GitHub 仓库的 README 与 `SKILL.md` 元数据，不执行仓库代码：

```bash
python skills/catalog-capabilities-zh/scripts/catalog_capabilities.py inspect-link --url https://github.com/owner/repo --format json
```

### 外部分组与链接根目录

如果一个目录只是外部 skills 的链接集合，先只读发现候选：

```bash
python skills/catalog-capabilities-zh/scripts/catalog_capabilities.py discover-links --root /path/to/skill-groups --format json
```

确认其中一个符号链接或 Windows junction 后，把它登记为独立根目录：

```bash
python skills/catalog-capabilities-zh/scripts/catalog_capabilities.py configure --skill-root ~/.codex/skills --linked-root /path/to/skill-groups/frontend --catalog ~/.codex/CAPABILITIES.zh-CN.md
```

`--linked-root` 是明确确认边界。扫描器不会自动进入分组目录里的其他链接。相同真实目标通过多个链接出现时会去重。普通目录分组过深时可以使用 `--scan-depth N`，该设置会保存到 profile。

## 安全边界

- GitHub README 和第三方 metadata 都作为不可信资料读取。
- 不执行仓库内脚本、README 命令或未知安装程序。
- 安装仍受 Agent 的联网、全局目录写入和账号权限审批约束。
- 插件缓存、市场候选、已启用和已安装状态分别记录。
- 扫描只进入用户确认的根目录；子级链接先发现、后确认，不会被静默跟随。
- 目录更新使用文件锁、原子替换和单份备份。

第一版完整支持 Codex。其他 Agent 可以在未来通过 adapter 扩展路径、安装器和验证规则。

## 开发与验证

```bash
python -m unittest discover -s tests -v
python /path/to/skill-creator/scripts/quick_validate.py skills/catalog-capabilities-zh
```

## License

MIT

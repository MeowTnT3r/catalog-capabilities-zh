// DeepSeek Harness host plugin for catalog-capabilities-zh.
//
// The plugin registers one runtime skill with `ctx.skills`. The skill body is
// read from `dsh/SKILL.md` at apply time and receives the absolute package
// path and Python CLI path, so the model does not have to guess where the
// installed package lives.

import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const name = 'catalog-capabilities-zh'
const inject = ['skills']

const packageRoot = dirname(fileURLToPath(new URL('../package.json', import.meta.url)))
const pythonScript = join(packageRoot, 'skills', 'catalog-capabilities-zh', 'scripts', 'catalog_capabilities.py')

const description =
  'Install-or-document router and searchable Chinese capability catalog for DeepSeek Harness skills and plugins. Use when the user asks to scan, install, update, find, categorize, or explain DSH capabilities in Chinese, provides a GitHub skill/plugin link, or explicitly invokes $catalog-capabilities-zh.'

const whenToUse =
  'Use when the user wants a Chinese catalog of DeepSeek Harness skills and plugins, asks which capability fits a task, wants a GitHub capability inspected, or explicitly asks to install or update a DSH capability.'

function stripFrontmatter(text) {
  if (!text.startsWith('---')) return text
  const end = text.indexOf('\n---', 3)
  if (end === -1) return text
  const after = text.indexOf('\n', end + 4)
  return after === -1 ? '' : text.slice(after + 1).replace(/^\n/, '')
}

function loadSkillContent() {
  const template = readFileSync(join(packageRoot, 'dsh', 'SKILL.md'), 'utf8')
  return stripFrontmatter(template)
    .split('<DSH_PACKAGE_ROOT>').join(packageRoot)
    .split('<DSH_SCRIPT>').join(pythonScript)
}

function apply(ctx) {
  const content = loadSkillContent()
  ctx.effect(function* () {
    yield ctx.skills.register({
      name,
      description,
      whenToUse,
      source: 'runtime',
      invocation: { modelInvocable: true, userInvocable: true },
      content,
      path: pythonScript,
      metadata: {
        ecosystem: 'dsh',
        package_root: packageRoot,
        python_script: pythonScript,
        source: 'runtime-skill',
      },
      resourceBase: { kind: 'directory', path: packageRoot },
    })
  }, 'catalog-capabilities-zh runtime skill')
}

export default { name, inject, apply }
export { name, inject, apply }

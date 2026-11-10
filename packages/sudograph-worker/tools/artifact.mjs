import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readdir, readFile, writeFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const hash = async path => createHash('sha256').update(await readFile(path)).digest('hex')
const names = (await readdir(join(root, 'dist'))).filter(name => /\.(?:js|d\.ts)$/.test(name)).sort()
const compiled = Object.fromEntries(await Promise.all(names.map(async name => [`dist/${name}`, await hash(join(root, 'dist', name))])))
const sourceNames = (await readdir(join(root, 'src'))).filter(name => name.endsWith('.ts')).sort()
const source = Object.fromEntries(await Promise.all(sourceNames.map(async name => [`src/${name}`, await hash(join(root, 'src', name))])))
const packageFiles = Object.fromEntries(await Promise.all(['package.json', 'npm-shrinkwrap.json', 'SOURCE.json', 'LICENSE'].map(async name => [name, await hash(join(root, name))])))
const manifest = { format: 'mosswork-sudograph-worker-artifact/v1', source, compiled, package_files: packageFiles }
const manifestPath = join(root, 'dist', 'manifest.json')
if (process.argv.length === 2) {
  await writeFile(manifestPath, JSON.stringify(manifest, null, 2) + '\n')
  console.log(`Recorded ${names.length} compiled files and ${sourceNames.length} source files`)
} else {
  assert(process.argv.length === 4 && process.argv[2] === '--verify', 'Use --verify <installed-package-directory>')
  const installed = resolve(process.argv[3])
  assert.deepEqual(JSON.parse(await readFile(manifestPath, 'utf8')), manifest, 'Source or build changed after manifest generation')
  assert.equal(await hash(join(installed, 'dist', 'manifest.json')), await hash(manifestPath), 'Installed manifest differs from the source build')
  for (const [name, expected] of Object.entries({ ...compiled, ...packageFiles })) {
    assert.equal(await hash(join(installed, name)), expected, `Installed bytes differ: ${name}`)
  }
  assert.deepEqual((await readdir(join(installed, 'dist'))).sort(), [...names, 'manifest.json'].sort(), 'Unexpected installed compiler files')
  console.log(`Verified ${names.length} installed compiled files against the current source build`)
}

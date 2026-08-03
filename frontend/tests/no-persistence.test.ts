import { readdirSync, readFileSync, statSync } from 'node:fs'
import { extname, join, resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

const sourceFiles = (directory: string): string[] =>
  readdirSync(directory).flatMap((name) => {
    const path = join(directory, name)
    return statSync(path).isDirectory() ? sourceFiles(path) : [path]
  })

const source = sourceFiles(resolve(process.cwd(), 'src'))
  .filter((path) => ['.ts', '.vue'].includes(extname(path)))
  .map((path) => readFileSync(path, 'utf8'))
  .join('\n')

describe('browser persistence boundary', () => {
  it('does not use browser persistence or service workers', () => {
    expect(source).not.toMatch(/\blocalStorage\b/)
    expect(source).not.toMatch(/\bsessionStorage\b/)
    expect(source).not.toMatch(/\bindexedDB\b/)
    expect(source).not.toMatch(/serviceWorker\s*\./)
  })
})
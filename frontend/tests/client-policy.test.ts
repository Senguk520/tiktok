import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

const source = readFileSync(resolve(process.cwd(), 'src/api/client.ts'), 'utf8')

describe('Core API client transport policy', () => {
  it('forces no-store and HttpOnly-cookie compatible requests', () => {
    expect(source).toContain("cache: 'no-store'")
    expect(source).toContain("credentials: 'include'")
    expect(source).toContain("redirect: 'error'")
  })

  it('does not embed an upstream provider endpoint', () => {
    expect(source).not.toMatch(/open-api\.tiktokglobalshop\.com/i)
    expect(source).not.toMatch(/cjdropshipping\.com/i)
    expect(source).not.toMatch(/1688\.com/i)
  })
})
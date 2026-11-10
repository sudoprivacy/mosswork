/** Wire data only; grants and canonical project registration remain host-owned. */
export interface WorkerBinding {
  authorityRef: string
  modelRef: string
  sourceRefs: string[]
}

export interface CanonicalContext {
  format: 'sudograph-agent-context/v1'
  revision: string
  context_digest: string
  text: string
}

/** Extracted from the existing worker's projectRegistry validator. */
export function validateOntologyContext(value: unknown): CanonicalContext {
  const context = value as CanonicalContext | null
  if (!context || context.format !== 'sudograph-agent-context/v1'
    || typeof context.revision !== 'string' || !/^[a-f0-9]{64}$/.test(context.revision)
    || typeof context.context_digest !== 'string' || !/^[a-f0-9]{64}$/.test(context.context_digest)
    || typeof context.text !== 'string' || !context.text.trim() || Buffer.byteLength(context.text) > 4 * 1024 * 1024) {
    throw new Error('Registered worker returned invalid canonical context')
  }
  return { format: context.format, revision: context.revision, context_digest: context.context_digest, text: context.text }
}

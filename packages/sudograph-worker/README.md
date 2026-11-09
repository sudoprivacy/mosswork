# Sudograph compiler worker

This portable Node worker exposes the four existing compiler MCP tools and the
private `sudograph-worker-review/v2` transport. It is extracted from the previous
Moss worker; `SOURCE.json` records the original source and dependency versions.
It uses the existing published Sudograph runtime and installed Python compiler.
The application version is unchanged. This package is private and is not
published automatically to npm. Node.js 22.21 or later is required by the
existing shared runtime; this is independent of the dashboard's Node 20 build.

```sh
npm ci
npm run build
npm test
npm pack --pack-destination /path/to/artifacts
```

`npm-shrinkwrap.json` locks the installed production dependency graph, including
the shared runtime tarball's integrity. `dist/manifest.json` records current
source and compiled-file hashes. After installing the tarball, verify it from
the source checkout before starting acceptance:

```sh
node tools/artifact.mjs --verify /path/to/installed/node_modules/@sudo/sudograph-worker
```

Install the resulting tarball in an isolated runtime directory, then invoke:

```sh
node node_modules/@sudo/sudograph-worker/dist/compilerContextWorkerCli.js /path/to/deployment.json
```

The deployment owns the configuration, original registered model/source refs,
compiler path, actor and binding address. Inject `SUDOGRAPH_WORKER_TOKEN` through
the existing secret manager. Registered PostgreSQL/MySQL sources additionally
use `SUDOGRAPH_SOURCE_PASSWORD`. Neither credential belongs in tool arguments or
the exported model. The strict configuration grammar remains in
`src/compilerWorkerConfig.ts`; do not construct it from model-produced paths or
connection text. The process announces `sudograph-worker-ready/v1` on stdout.

The existing `tools/accept_sudograph_worker.py` accepts the installed CLI as
`--worker`, with its verified `--worker-sha256` and installed compiler path. It
exercises actual encrypted connector selection, MCP reads/writes and revision
checks. `--emit-review-fixture` exports compiler HTML and exact model pointers
for browser integration. This is separate from the package's refusal tests.

A deployment service token authorizes this private worker transport. Current
human/org/Agent/session, full registered model/source/projection grants, native
host admission and durable audit remain host requirements. This package creates
no native session, user grant, comment dispatcher or source-write interface.
Its tests and package build do not qualify managed model execution, customer
databases, authenticated comments, the complete active backend or production.

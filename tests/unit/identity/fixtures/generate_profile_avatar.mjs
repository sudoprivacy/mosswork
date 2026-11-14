// Public synthetic rasters only; execute the pinned original sharp normalizer.
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const sourcePath = resolve(process.argv[2]);
const sharpPath = resolve(process.argv[3]);
const outputPath = resolve(process.argv[4]);
const source = readFileSync(sourcePath, 'utf8');
const originalPath = join(dirname(sourcePath), 'original-avatar.ts');
writeFileSync(originalPath, source
  .replace("import { AuthServiceError } from '../auth/service.js'",
    'class AuthServiceError extends Error { statusCode:number; constructor(statusCode:number,message:string) { super(message); this.statusCode=statusCode; } }')
  .replace("import('sharp')", `import(${JSON.stringify(pathToFileURL(sharpPath).href)})`));
const { normalizeProfileAvatar } = await import(pathToFileURL(originalPath).href);
const { default: sharp } = await import(pathToFileURL(sharpPath).href);
const image = sharp({ create: { width: 640, height: 320, channels: 4, background: '#30a070' } });
const png = await image.clone().png().toBuffer();
const jpeg = await image.clone().jpeg().toBuffer();
const webp = await image.clone().webp().toBuffer();
const oriented = await image.clone().withMetadata({ orientation: 6 }).jpeg().toBuffer();
const pixels = Buffer.alloc(640 * 320 * 3);
for (let y = 0; y < 320; y++) for (let x = 0; x < 640; x++) {
  const i = (y * 640 + x) * 3;
  pixels[i] = x < 320 ? 210 : 30;
  pixels[i + 1] = y < 160 ? 190 : 40;
  pixels[i + 2] = x < 320 ? 30 : 210;
}
const pattern = sharp(pixels, { raw: { width: 640, height: 320, channels: 3 } });
const patternPng = await pattern.clone().png().toBuffer();
const patternOriented = await pattern.clone().withMetadata({ orientation: 6 }).jpeg().toBuffer();
const transparent = await sharp({ create: { width: 16, height: 16, channels: 4,
  background: { r: 48, g: 160, b: 112, alpha: 0.5 } } }).png().toBuffer();
const data = (format, bytes) => `data:image/${format};base64,${bytes.toString('base64')}`;
const inputs = [
  ['clear', null], ['boolean', false], ['object', {}], ['number', 42],
  ['empty', ''], ['url', 'https://example.test/avatar.png'], ['local_path', 'C:/avatar.png'],
  ['svg', data('svg+xml', Buffer.from('<svg/>'))], ['gif', data('gif', Buffer.from('GIF89a'))],
  ['unsupported_jpg_alias', data('jpg', jpeg)], ['uppercase', data('PNG', png)],
  ['leading_space', ' ' + data('png', png)], ['base64_space', 'data:image/png;base64,AA AA'],
  ['invalid_raster', data('png', Buffer.from('public invalid raster'))],
  ['truncated_png', data('png', png.subarray(0, 48))],
  ['wrong_declared_format', data('png', jpeg)],
  ['png', data('png', png)], ['jpeg', data('jpeg', jpeg)], ['webp', data('webp', webp)],
  ['exif_orientation', data('jpeg', oriented)],
  ['pattern_crop', data('png', patternPng)],
  ['oriented_pattern', data('jpeg', patternOriented)],
  ['transparency', data('png', transparent)],
  ['unpadded_base64', data('png', png).replace(/=+$/, '')],
  ['oversize', { repeat: 'a', bytes: 2 * 1024 * 1024 + 1 }],
];
const cases = [];
for (const [name, specification] of inputs) {
  const input = name === 'oversize'
    ? data('png', Buffer.alloc(specification.bytes, specification.repeat)) : specification;
  let status = 200, normalized = null, metadata = null, center = null, samples = null;
  try {
    normalized = await normalizeProfileAvatar(input);
    if (normalized !== null) {
      const bytes = Buffer.from(normalized.split(',')[1], 'base64');
      const raster = sharp(bytes);
      const info = await raster.metadata();
      metadata = { format: info.format, width: info.width, height: info.height, hasAlpha: !!info.hasAlpha };
      center = [...await raster.extract({ left: 128, top: 128, width: 1, height: 1 })
        .removeAlpha().raw().toBuffer()];
      samples = [];
      for (const [x, y] of [[32, 32], [224, 32], [32, 224], [224, 224]]) {
        const pixel = await sharp(bytes).extract({ left: x, top: y, width: 1, height: 1 })
          .ensureAlpha().raw().toBuffer();
        samples.push({ x, y, rgba: [...pixel] });
      }
    }
  } catch (error) { status = error.statusCode ?? 500; }
  cases.push({ name, input: specification, status, metadata, center, samples });
}
writeFileSync(outputPath, JSON.stringify({
  sourceCommit: 'd0b84d4c8fd5905d9ec3f7c160f2ce633f535d0d',
  sourceSha256: createHash('sha256').update(readFileSync(sourcePath)).digest('hex'),
  sharpVersion: sharp.versions.sharp,
  outputBoundary: 'Compare status, decoded dimensions/format/color; no cross-encoder byte identity.',
  cases,
}, null, 2) + '\n');
console.log(JSON.stringify({ cases: cases.length, passed: cases.filter(x => x.status === 200).length,
  outputSha256: createHash('sha256').update(readFileSync(outputPath)).digest('hex') }));

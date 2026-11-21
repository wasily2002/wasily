/**
 * 从 node_modules/@fontsource/* 提取 woff2 文件并上传到 R2
 * 运行: npx tsx scripts/sync-fonts.ts
 */
import { readdirSync, readFileSync, existsSync } from 'fs';
import { join } from 'path';

const FONTS_DIR = join(process.cwd(), 'node_modules', '@fontsource');
const OUTPUT_MANIFEST = join(process.cwd(), 'dist', 'upload-manifest.json');

interface UploadItem {
  localPath: string;
  r2Key: string;
}

function collectFonts(): UploadItem[] {
  const items: UploadItem[] = [];
  const packages = readdirSync(FONTS_DIR).filter((d) => !d.startsWith('.'));

  for (const pkg of packages) {
    const filesDir = join(FONTS_DIR, pkg, 'files');
    if (!existsSync(filesDir)) continue;

    const woff2Files = readdirSync(filesDir).filter(
      (f) => f.endsWith('.woff2') && !f.includes('-subset')
    );

    for (const file of woff2Files) {
      // 将 @fontsource/noto-sans-sc/files/noto-sans-sc-chinese-simplified-400-normal.woff2
      // 映射为 fonts/noto-sans-sc/400-normal.woff2
      const fontId = pkg.replace('@fontsource/', '');
      const weightMatch = file.match(/-(\d+)-(normal|italic)\.woff2$/);
      if (!weightMatch) continue;

      const r2Key = `fonts/${fontId}/${weightMatch[1]}-${weightMatch[2]}.woff2`;
      items.push({
        localPath: join(filesDir, file),
        r2Key,
      });
    }
  }

  return items;
}

// 生成上传清单供 CI 使用
const manifest = collectFonts();
console.log(`Found ${manifest.length} font files to upload:`);
manifest.forEach((item) => console.log(`  ${item.r2Key}`));

// 实际上传逻辑使用 wrangler r2 object put 或 S3 SDK
// 此处仅生成清单，CI 中执行:
// for item in manifest: wrangler r2 object put font-assets/$r2Key --file=$localPath
console.log(`\nManifest written. Use 'wrangler r2 object put' to upload.`);
/*
 * @Author: error: error: git config user.name & please set dead value or install git && error: git config user.email & please set dead value or install git & please set dead value or install git
 * @Date: 2026-10-07 16:17:12
 * @LastEditors: error: error: git config user.name & please set dead value or install git && error: git config user.email & please set dead value or install git & please set dead value or install git
 * @LastEditTime: 2026-10-07 16:22:26
 * @FilePath: \项目介绍\wasily\CDN\font-cdn\src\index.ts
 * @Description: 这是默认设置,请设置`customMade`, 打开koroFileHeader查看配置 进行设置: https://github.com/OBKoro1/koro1FileHeader/wiki/%E9%85%8D%E7%BD%AE
 */
import { Hono } from 'hono';
import { parseFontRequest, generateCSS } from './css-generator';
import { FONT_HEADERS, CSS_HEADERS, ERROR_HEADERS } from './headers';
import { FONT_REGISTRY } from './font-metadata';

type Bindings = {
  FONT_BUCKET: R2Bucket;
};

const app = new Hono<{ Bindings: Bindings }>();

// ✅ Google Fonts CSS2 兼容端点
app.get('/css2', async (c) => {
  const familyParam = c.req.query('family');
  if (!familyParam) {
    return c.text('Missing "family" parameter', 400, ERROR_HEADERS);
  }

  const parsed = parseFontRequest(familyParam);
  if (!parsed) {
    return c.text(`Font family not found: ${familyParam}`, 404, ERROR_HEADERS);
  }

  const css = generateCSS(parsed);
  if (!css) {
    return c.text('No matching variants found', 404, ERROR_HEADERS);
  }

  return c.text(css, 200, CSS_HEADERS);
});

// ✅ 字体文件直接访问
app.get('/fonts/:path{.+}', async (c) => {
  const path = c.req.param('path');
  const object = await c.env.FONT_BUCKET.get(`fonts/${path}`);

  if (!object) {
    return c.text('Font file not found', 404, ERROR_HEADERS);
  }

  const contentType = path.endsWith('.woff2') ? 'font/woff2'
    : path.endsWith('.woff') ? 'font/woff'
    : 'application/octet-stream';

  return new Response(object.body, {
    headers: { ...FONT_HEADERS, 'Content-Type': contentType },
  });
});

// ✅ 可用字体列表 API
app.get('/api/families', (c) => {
  const families = Object.values(FONT_REGISTRY).map((f) => ({
    family: f.family,
    id: f.id,
    weights: [...new Set(f.variants.map((v) => v.weight))],
    fallback: f.fallback,
  }));
  return c.json(families, 200, {
    'Cache-Control': 'public, max-age=3600',
    'Access-Control-Allow-Origin': '*',
  });
});

// ✅ 健康检查
app.get('/', (c) => {
  return c.json({
    service: 'Open Font CDN',
    version: '1.0.0',
    families: Object.keys(FONT_REGISTRY).length,
    docs: 'https://github.com/your-org/open-font-cdn',
  });
});

export default app;
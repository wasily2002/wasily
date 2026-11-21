import { FONT_REGISTRY, type FontFamily, type FontVariant } from './font-metadata';

interface ParsedRequest {
  familyId: string;
  weights: string[];
  display: string;
  subsets?: string[];
}

/**
 * 解析 Google Fonts CSS2 格式的请求
 * 例: Noto+Sans+SC:wght@400;700&display=swap
 */
export function parseFontRequest(query: string): ParsedRequest | null {
  const match = query.match(/^([^:]+)(?::(.+))?$/);
  if (!match) return null;

  const familyRaw = decodeURIComponent(match[1]).replace(/\+/g, ' ');
  const params = match[2] || '';

  // 查找匹配的字体 ID
  const familyId = Object.keys(FONT_REGISTRY).find(
    (id) => FONT_REGISTRY[id].family.toLowerCase() === familyRaw.toLowerCase()
  );
  if (!familyId) return null;

  const weights: string[] = [];
  let display = 'swap';

  for (const param of params.split('&')) {
    const [key, value] = param.split('=');
    if (key === 'wght') {
      weights.push(...value.split(';'));
    } else if (key === 'display') {
      display = value;
    }
  }

  return { familyId, weights: weights.length ? weights : ['400'], display };
}

/**
 * 根据请求参数生成完整的 @font-face CSS
 */
export function generateCSS(req: ParsedRequest): string | null {
  const font = FONT_REGISTRY[req.familyId];
  if (!font) return null;

  const matchedVariants = font.variants.filter((v) =>
    req.weights.includes(v.weight) && v.style === 'normal'
  );

  if (matchedVariants.length === 0) return null;

  const blocks = matchedVariants.map((variant) => buildFontFace(font, variant, req.display));
  return blocks.join('\n\n');
}

function buildFontFace(font: FontFamily, variant: FontVariant, display: string): string {
  const lines = [
    `@font-face {`,
    `  font-family: '${font.family}';`,
    `  font-style: ${variant.style};`,
    `  font-weight: ${variant.weight};`,
    `  font-display: ${display};`,
    `  src: url('/${variant.path}') format('woff2');`,
  ];

  if (variant.unicodeRange) {
    lines.push(`  unicode-range: ${variant.unicodeRange};`);
  }

  lines.push('}');
  return lines.join('\n');
}
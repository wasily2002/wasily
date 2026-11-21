export interface FontVariant {
  weight: string;
  style: string;
  path: string;         // R2 中的对象键
  unicodeRange?: string;
}

export interface FontFamily {
  family: string;
  id: string;           // URL 中使用的标识符
  variants: FontVariant[];
  fallback: string;
}

/**
 * 添加新字体只需在此注册
 * path 对应 R2 桶中的文件路径: fonts/{id}/{weight}-{style}.woff2
 */
export const FONT_REGISTRY: Record<string, FontFamily> = {
  'noto-sans-sc': {
    family: 'Noto Sans SC',
    id: 'noto-sans-sc',
    fallback: 'sans-serif',
    variants: [
      { weight: '400', style: 'normal', path: 'fonts/noto-sans-sc/400-normal.woff2', unicodeRange: 'U+4E00-9FFF,U+3000-303F,U+FF00-FFEF' },
      { weight: '700', style: 'normal', path: 'fonts/noto-sans-sc/700-normal.woff2', unicodeRange: 'U+4E00-9FFF,U+3000-303F,U+FF00-FFEF' },
      { weight: '400', style: 'normal', path: 'fonts/noto-sans-sc/400-normal-latin.woff2', unicodeRange: 'U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+2000-206F' },
    ],
  },
  'inter': {
    family: 'Inter',
    id: 'inter',
    fallback: 'system-ui, sans-serif',
    variants: [
      { weight: '400', style: 'normal', path: 'fonts/inter/400-normal.woff2' },
      { weight: '700', style: 'normal', path: 'fonts/inter/700-normal.woff2' },
      { weight: '400', style: 'italic', path: 'fonts/inter/400-italic.woff2' },
    ],
  },
  'jetbrains-mono': {
    family: 'JetBrains Mono',
    id: 'jetbrains-mono',
    fallback: 'monospace',
    variants: [
      { weight: '400', style: 'normal', path: 'fonts/jetbrains-mono/400-normal.woff2' },
      { weight: '700', style: 'normal', path: 'fonts/jetbrains-mono/700-normal.woff2' },
    ],
  },
};
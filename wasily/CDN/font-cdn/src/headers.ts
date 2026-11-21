export const FONT_HEADERS: Record<string, string> = {
  'Cache-Control': 'public, max-age=31536000, immutable',
  'Access-Control-Allow-Origin': '*',
  'X-Content-Type-Options': 'nosniff',
  'Cross-Origin-Resource-Policy': 'cross-origin',
  'Timing-Allow-Origin': '*',
};

export const CSS_HEADERS: Record<string, string> = {
  'Cache-Control': 'public, max-age=86400, stale-while-revalidate=604800',
  'Content-Type': 'text/css; charset=utf-8',
  'Access-Control-Allow-Origin': '*',
  'X-Content-Type-Options': 'nosniff',
};

export const ERROR_HEADERS: Record<string, string> = {
  'Cache-Control': 'public, max-age=60',
  'Content-Type': 'text/plain; charset=utf-8',
};
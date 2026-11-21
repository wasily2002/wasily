font-cdn/
├── packages/
│   ├── builder/          # 字体处理脚本
│   │   ├── src/
│   │   │   ├── fetcher.ts      # 从 npm/GitHub 拉取字体
│   │   │   ├── converter.ts    # woff2/brotli 转换
│   │   │   ├── subsetter.ts    # 中文子集化 (cn-font-split)
│   │   │   └── manifest.ts     # 生成版本清单
│   │   └── package.json
│   └── api/              # CDN 服务代码
│       ├── src/
│       │   ├── router.ts       # /css2, /font-file 路由
│       │   ├── cache.ts        # 多级缓存策略
│       │   └── headers.ts      # CORS/CSP/安全头
│       └── wrangler.toml
├── fonts/                # 构建产物（可选提交或 CI 生成）
│   ├── noto-sans-sc/
│   │   ├── 400.woff2
│   │   ├── 700.woff2
│   │   └── metadata.json
│   └── ...
├── .github/workflows/
│   ├── build.yml         # 定时同步字体
│   └── deploy.yml        # 自动部署到 Workers
├── LICENSE               # MIT / OFL-1.1
└── README.md             # 使用说明 + API 文档
# 日本地図の境界データ

`japan-map-data.js` は、Data of Japan の `japan.geojson` を
`scripts/build_japan_map.py` で表示用SVGパスへ変換したものです。

- 変換元: https://github.com/dataofjapan/land
- 原典: 地球地図日本（国土地理院）
- 原典案内: https://www.gsi.go.jp/kankyochiri/gm_jpn.html

変換元の案内に従い、サイトのフッターに原典を表示しています。

再生成する場合:

```bash
curl -fL https://raw.githubusercontent.com/dataofjapan/land/master/japan.geojson \
  -o data/japan-prefectures-source.geojson
python3 scripts/build_japan_map.py \
  data/japan-prefectures-source.geojson japan-map-data.js
```

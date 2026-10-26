# JP stimulus mini-set — ja-hybrid classifier (#164, prereg §14)

> 測定値のみ。正解はモデル2者の下書き参照ラベル（人間の盲検ラベルではない）。発話は合成の典型例なので、**実際の会話では下がる可能性が高い**。`rule` / `sokudan` はコミット済みの `classifier-outputs.json`、ja-hybrid は `outputs.json`（mlask-official 0.5.0、ipadic 1.0.0）。分割: stratified by gen_target; ids sorted; alternate dev,test starting with dev。

## dev

- 発話 70 件。core6 参照 n=56（要人間判定 14 件は除外）、label 参照 n=65（除外 5 件）

| 分類器 | core6 正解率 | 6カテゴリ マクロ適合率 / 再現率 | 検出 適合率 / 再現率 | 失敗・苛立ち・批判・圧力 → loss | 脅威の解消 → 出力 |
| --- | --- | --- | --- | --- | --- |
| ja-hybrid | 0.89 | 0.94 / 0.94 | 1.00 / 0.90 | 0/20 | achievement 1, joy 1, none 3（n=5） |
| ja-hybrid-patterns-only | 0.86 | 0.92 / 0.89 | 1.00 / 0.88 | 0/20 | achievement 1, joy 1, none 3（n=5） |
| rule | 0.25 | 0.50 / 0.04 | 0.50 / 0.02 | 0/20 | none 5（n=5） |
| sokudan | 0.71 | 0.69 / 0.94 | 0.81 / 1.00 | 5/20 | achievement 3, threat 2（n=5） |

カテゴリ別（適合率 / 再現率、参照件数）:

| core6 | 参照 | ja-hybrid | ja-hybrid-patterns-only | rule | sokudan |
| --- | --- | --- | --- | --- | --- |
| joy | 5 | 0.83 / 1.00 | 0.83 / 1.00 | – / 0.00 | 0.83 / 1.00 |
| threat | 18 | 1.00 / 0.67 | 1.00 / 0.61 | – / 0.00 | 0.92 / 0.67 |
| achievement | 5 | 0.83 / 1.00 | 0.71 / 1.00 | – / 0.00 | 0.45 / 1.00 |
| loss | 5 | 1.00 / 1.00 | 1.00 / 1.00 | – / 0.00 | 0.71 / 1.00 |
| uncertainty | 5 | 1.00 / 1.00 | 1.00 / 1.00 | 0.00 / 0.00 | 0.56 / 1.00 |
| connection | 4 | 1.00 / 1.00 | 1.00 / 0.75 | 1.00 / 0.25 | 0.67 / 1.00 |
| none | 14 | 0.78 / 1.00 | 0.74 / 1.00 | 0.24 / 0.93 | 1.00 / 0.29 |

`discourse_tags`（ja-hybrid。エンジンは使わない。参照は単一ラベルなので複数タグは FP に数える）:

| タグ | 参照の欄 | 参照件数 | 付与 | TP | 適合率 | 再現率 |
| --- | --- | --- | --- | --- | --- | --- |
| failure | label | 6 | 8 | 6 | 0.75 | 1.00 |
| frustration | label | 4 | 5 | 4 | 0.80 | 1.00 |
| criticism | label | 5 | 5 | 5 | 1.00 | 1.00 |
| pressure | label | 5 | 5 | 5 | 1.00 | 1.00 |
| resolved | label | 5 | 5 | 5 | 1.00 | 1.00 |
| escalated | polarity=worsening | 10 | 7 | 7 | 1.00 | 0.70 |

`discourse_tags`（ja-hybrid-patterns-only。エンジンは使わない。参照は単一ラベルなので複数タグは FP に数える）:

| タグ | 参照の欄 | 参照件数 | 付与 | TP | 適合率 | 再現率 |
| --- | --- | --- | --- | --- | --- | --- |
| failure | label | 6 | 8 | 6 | 0.75 | 1.00 |
| frustration | label | 4 | 3 | 3 | 1.00 | 0.75 |
| criticism | label | 5 | 5 | 5 | 1.00 | 1.00 |
| pressure | label | 5 | 5 | 5 | 1.00 | 1.00 |
| resolved | label | 5 | 5 | 5 | 1.00 | 1.00 |
| escalated | polarity=worsening | 10 | 7 | 7 | 1.00 | 0.70 |

拡張ラベル → 出力（ja-hybrid）:

| 参照 label | 出力（件数） |
| --- | --- |
| achievement | achievement 5 |
| connection | connection 4 |
| criticism | none 5 |
| failure | none 5, threat 1 |
| frustration | threat 4 |
| joy | joy 5 |
| loss | loss 5 |
| none | none 6 |
| pressure | none 5 |
| threat | threat 10 |
| threat_resolved | achievement 1, joy 1, none 3 |
| uncertainty | uncertainty 5 |

## test

- 発話 70 件。core6 参照 n=64（要人間判定 6 件は除外）、label 参照 n=69（除外 1 件）

| 分類器 | core6 正解率 | 6カテゴリ マクロ適合率 / 再現率 | 検出 適合率 / 再現率 | 失敗・苛立ち・批判・圧力 → loss | 脅威の解消 → 出力 |
| --- | --- | --- | --- | --- | --- |
| ja-hybrid | 0.59 | 0.61 / 0.46 | 0.88 / 0.73 | 0/20 | none 4, threat 2（n=6） |
| ja-hybrid-patterns-only | 0.61 | 0.71 / 0.45 | 0.92 / 0.59 | 0/20 | none 4, threat 2（n=6） |
| rule | 0.36 | – / 0.00 | – / 0.00 | 0/20 | none 6（n=6） |
| sokudan | 0.62 | 0.65 / 0.84 | 0.71 / 0.95 | 5/20 | achievement 2, joy 1, threat 3（n=6） |

カテゴリ別（適合率 / 再現率、参照件数）:

| core6 | 参照 | ja-hybrid | ja-hybrid-patterns-only | rule | sokudan |
| --- | --- | --- | --- | --- | --- |
| joy | 5 | 0.25 / 0.40 | 0.50 / 0.20 | – / 0.00 | 0.62 / 1.00 |
| threat | 16 | 0.67 / 0.50 | 0.78 / 0.44 | – / 0.00 | 0.92 / 0.75 |
| achievement | 4 | 0.75 / 0.75 | 0.50 / 0.75 | – / 0.00 | 0.57 / 1.00 |
| loss | 6 | 0.50 / 0.33 | 1.00 / 0.50 | – / 0.00 | 0.40 / 0.67 |
| uncertainty | 5 | 0.50 / 0.40 | 0.50 / 0.40 | – / 0.00 | 0.36 / 1.00 |
| connection | 5 | 1.00 / 0.40 | 1.00 / 0.40 | – / 0.00 | 1.00 / 0.60 |
| none | 23 | 0.63 / 0.83 | 0.55 / 0.91 | 0.36 / 1.00 | 0.78 / 0.30 |

`discourse_tags`（ja-hybrid。エンジンは使わない。参照は単一ラベルなので複数タグは FP に数える）:

| タグ | 参照の欄 | 参照件数 | 付与 | TP | 適合率 | 再現率 |
| --- | --- | --- | --- | --- | --- | --- |
| failure | label | 5 | 9 | 3 | 0.33 | 0.60 |
| frustration | label | 5 | 7 | 4 | 0.57 | 0.80 |
| criticism | label | 5 | 2 | 1 | 0.50 | 0.20 |
| pressure | label | 5 | 4 | 3 | 0.75 | 0.60 |
| resolved | label | 6 | 5 | 4 | 0.80 | 0.67 |
| escalated | polarity=worsening | 10 | 4 | 4 | 1.00 | 0.40 |

`discourse_tags`（ja-hybrid-patterns-only。エンジンは使わない。参照は単一ラベルなので複数タグは FP に数える）:

| タグ | 参照の欄 | 参照件数 | 付与 | TP | 適合率 | 再現率 |
| --- | --- | --- | --- | --- | --- | --- |
| failure | label | 5 | 9 | 3 | 0.33 | 0.60 |
| frustration | label | 5 | 4 | 4 | 1.00 | 0.80 |
| criticism | label | 5 | 2 | 1 | 0.50 | 0.20 |
| pressure | label | 5 | 4 | 3 | 0.75 | 0.60 |
| resolved | label | 6 | 5 | 4 | 0.80 | 0.67 |
| escalated | polarity=worsening | 10 | 4 | 4 | 1.00 | 0.40 |

拡張ラベル → 出力（ja-hybrid）:

| 参照 label | 出力（件数） |
| --- | --- |
| achievement | achievement 3, joy 1 |
| connection | connection 2, joy 1, none 2 |
| criticism | none 5 |
| failure | joy 1, none 4 |
| frustration | achievement 1, none 1, threat 3 |
| joy | joy 2, loss 1, none 1, threat 1 |
| loss | loss 2, none 2, threat 1 |
| none | joy 1, loss 1, none 7 |
| pressure | none 5 |
| threat | joy 1, none 2, threat 5, uncertainty 2 |
| threat_resolved | none 4, threat 2 |
| uncertainty | joy 1, none 1, threat 1, uncertainty 2 |


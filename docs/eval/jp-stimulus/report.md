# JP stimulus mini-set v1 — results (#164, #143 stage-2 prep)

> 測定値のみ。ラベルは**モデル2者の下書き**（Astra と Opus 5.5 が独立・盲検で付けたもの）で、人間の盲検ラベルではない。設計判断（カテゴリ追加かイベント化か）はここでは行わない。基準は `docs/eval/emotion-ablation-prereg.md` §13（実測前に固定）。

- 発話: 140 件（合成。生成 = Sonnet 5.5）
- 生成意図の内訳: achievement 10, connection 10, criticism 10, failure 10, frustration 10, joy 10, loss 10, neutral 10, noise 10, pressure 10, threat 10, threat_escalated 10, threat_resolved 10, uncertainty 10
- PII スクリーン該当: 0 件
- ラベラー astra: `global.openai.gpt-6-astra`、calls 7、ラベルなし 0
- ラベラー opus: `global.anthropic.claude-opus-5-5`、calls 7、ラベルなし 0

## 1. 一致率（Cohen の κ）

| 欄 | n | κ | 95% 区間 | 単純一致 | 判定（§13.4） | 定義見直し候補（PSA<0.6） |
| --- | --- | --- | --- | --- | --- | --- |
| label | 140 | 0.95 | 0.91–0.98 | 0.96 | adopt-as-draft-reference | – |
| core6 | 140 | 0.82 | 0.74–0.89 | 0.86 | adopt-as-draft-reference | – |
| polarity | 140 | 0.70 | 0.57–0.81 | 0.84 | adopt-as-draft-reference | – |

### カテゴリ別の特異的一致（label）

| label | Astra 件数 | Opus 件数 | 両者 | PSA |
| --- | --- | --- | --- | --- |
| joy | 11 | 10 | 10 | 0.95 |
| threat | 21 | 20 | 20 | 0.98 |
| achievement | 10 | 9 | 9 | 0.95 |
| loss | 10 | 10 | 10 | 1.00 |
| uncertainty | 10 | 10 | 10 | 1.00 |
| connection | 9 | 10 | 9 | 0.95 |
| failure | 13 | 12 | 11 | 0.88 |
| frustration | 10 | 9 | 9 | 0.95 |
| criticism | 10 | 10 | 10 | 1.00 |
| pressure | 10 | 10 | 10 | 1.00 |
| threat_resolved | 11 | 11 | 11 | 1.00 |
| none | 15 | 19 | 15 | 0.88 |

### 混同行列（label、行 = Astra / 列 = Opus）

| A＼B | joy | threat | achievement | loss | uncertainty | connection | failure | frustration | criticism | pressure | threat_resolved | none |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| joy | 10 | · | · | · | · | · | · | · | · | · | · | 1 |
| threat | · | 20 | · | · | · | · | · | · | · | · | · | 1 |
| achievement | · | · | 9 | · | · | 1 | · | · | · | · | · | · |
| loss | · | · | · | 10 | · | · | · | · | · | · | · | · |
| uncertainty | · | · | · | · | 10 | · | · | · | · | · | · | · |
| connection | · | · | · | · | · | 9 | · | · | · | · | · | · |
| failure | · | · | · | · | · | · | 11 | · | · | · | · | 2 |
| frustration | · | · | · | · | · | · | 1 | 9 | · | · | · | · |
| criticism | · | · | · | · | · | · | · | · | 10 | · | · | · |
| pressure | · | · | · | · | · | · | · | · | · | 10 | · | · |
| threat_resolved | · | · | · | · | · | · | · | · | · | · | 11 | · |
| none | · | · | · | · | · | · | · | · | · | · | · | 15 |

参照ラベル（一致）/ 要人間判定: label 134/6、core6 120/20、polarity 118/22

## 2. 生成意図との一致（参考指標。参照ラベルの決定には使わない）

- astra: 131/140
- opus: 135/140
- reference: 130/134

| 生成意図 | n | 一致数 | 参照ラベルの内訳（None = 要人間判定） |
| --- | --- | --- | --- |
| joy | 10 | 10 | joy 10 |
| threat | 10 | 10 | threat 10 |
| achievement | 10 | 10 | achievement 9, threat_resolved 1 |
| loss | 10 | 10 | loss 10 |
| uncertainty | 10 | 10 | uncertainty 10 |
| connection | 10 | 9 | None 1, connection 9 |
| failure | 10 | 10 | failure 9, frustration 1 |
| frustration | 10 | 9 | None 1, failure 1, frustration 8 |
| criticism | 10 | 10 | criticism 10 |
| pressure | 10 | 10 | pressure 10 |
| threat_resolved | 10 | 10 | threat_resolved 10 |
| threat_escalated | 10 | 10 | threat 10 |
| neutral | 10 | 8 | None 2, none 8 |
| noise | 10 | 8 | None 2, failure 1, none 7 |

### 欠落場面を現行6カテゴリだけで表すと（label 一致項目の core6、ラベラー別）

| label | n | Astra の core6 | Opus の core6 |
| --- | --- | --- | --- |
| failure | 11 | none 3, threat 8 | none 7, threat 2, uncertainty 2 |
| frustration | 9 | loss 1, none 4, threat 4 | loss 1, none 3, threat 4, uncertainty 1 |
| criticism | 10 | none 10 | loss 1, none 8, threat 1 |
| pressure | 10 | none 10 | connection 1, none 9 |
| threat_resolved | 11 | joy 1, threat 10 | joy 1, none 1, threat 9 |

## 3. 現行分類器（参照ラベルに対して）

### rule

core6 参照 n=120、正解率 0.31、6カテゴリのマクロ適合率 0.50 / 再現率 0.02、検出（none 以外）適合率 0.50 / 再現率 0.01

| core6 | 参照件数 | 予測件数 | TP | 適合率 | 再現率 | F1 |
| --- | --- | --- | --- | --- | --- | --- |
| joy | 10 | 0 | 0 | – | 0.00 | 0.00 |
| threat | 34 | 0 | 0 | – | 0.00 | 0.00 |
| achievement | 9 | 0 | 0 | – | 0.00 | 0.00 |
| loss | 11 | 0 | 0 | – | 0.00 | 0.00 |
| uncertainty | 10 | 1 | 0 | 0.00 | 0.00 | 0.00 |
| connection | 9 | 1 | 1 | 1.00 | 0.11 | 0.20 |
| none | 37 | 118 | 36 | 0.31 | 0.97 | 0.46 |

拡張ラベル（参照 n=134）→ 分類器の出力:

| 参照 label | 分類器の出力（件数） |
| --- | --- |
| achievement | none 9 |
| connection | connection 1, none 8 |
| criticism | none 10 |
| failure | none 11 |
| frustration | none 9 |
| joy | none 10 |
| loss | none 10 |
| none | none 14, uncertainty 1 |
| pressure | none 10 |
| threat | none 20 |
| threat_resolved | none 11 |
| uncertainty | none 10 |

脅威の極性（参照 label/polarity → 出力）: 
- threat/none: none 1
- threat/worsening: none 18
- threat_resolved/resolving: none 11

### sokudan

core6 参照 n=120、正解率 0.67、6カテゴリのマクロ適合率 0.65 / 再現率 0.88、検出（none 以外）適合率 0.76 / 再現率 0.98

| core6 | 参照件数 | 予測件数 | TP | 適合率 | 再現率 | F1 |
| --- | --- | --- | --- | --- | --- | --- |
| joy | 10 | 14 | 10 | 0.71 | 1.00 | 0.83 |
| threat | 34 | 26 | 24 | 0.92 | 0.71 | 0.80 |
| achievement | 9 | 18 | 9 | 0.50 | 1.00 | 0.67 |
| loss | 11 | 17 | 9 | 0.53 | 0.82 | 0.64 |
| uncertainty | 10 | 23 | 10 | 0.43 | 1.00 | 0.61 |
| connection | 9 | 9 | 7 | 0.78 | 0.78 | 0.78 |
| none | 37 | 13 | 11 | 0.85 | 0.30 | 0.44 |

拡張ラベル（参照 n=134）→ 分類器の出力:

| 参照 label | 分類器の出力（件数） |
| --- | --- |
| achievement | achievement 9 |
| connection | connection 7, joy 2 |
| criticism | achievement 1, loss 2, none 4, threat 1, uncertainty 2 |
| failure | achievement 2, loss 4, none 2, threat 1, uncertainty 2 |
| frustration | connection 1, loss 3, none 1, threat 4 |
| joy | joy 10 |
| loss | loss 9, none 1 |
| none | achievement 1, joy 1, none 6, threat 1, uncertainty 6 |
| pressure | achievement 3, connection 1, joy 1, loss 1, none 2, uncertainty 2 |
| threat | loss 1, threat 17, uncertainty 2 |
| threat_resolved | achievement 5, joy 1, threat 5 |
| uncertainty | uncertainty 10 |

脅威の極性（参照 label/polarity → 出力）: 
- threat/none: threat 1
- threat/worsening: threat 15, uncertainty 2, loss 1
- threat_resolved/resolving: achievement 5, joy 1, threat 5

## 4. 呼び出し・トークン

| 役割 | モデル | calls | 再試行 | パース失敗 | 入力文字数 | 入力 tok | 出力 tok | 概算 USD |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| generator | `global.anthropic.claude-sonnet-5-5` | 15 | 1 | 1 | 8667 | 7889 | 11923 | 0.135 |
| astra | `global.openai.gpt-6-astra` | 7 | 0 | 0 | 21305 | 14（報告値が不自然） | 7269 | 単価不明 |
| opus | `global.anthropic.claude-opus-5-5` | 7 | 0 | 0 | 21305 | 16800 | 8341 | 0.292 |

## 5. 人間の抽出検証用サンプル（§13.6、判定は未実施）

- 一致項目から 14 件: u005, u008, u013, u018, u022, u023, u037, u055, u080, u086, u088, u102, u127, u134
- 不一致項目から 6 件: u012, u016, u017, u019, u042, u067

## 6. 不一致ケース（35 件、要人間判定）

| id | 発話 | 欄 | Astra | Opus |
| --- | --- | --- | --- | --- |
| u005 | 今日、英検2級の過去問を初めて満点で解くことができました。毎日少しずつ続けた成果が出てうれしいです。 | polarity | achievement/achievement/resolving | achievement/achievement/none |
| u006 | 亡くなった父のサーバーを整理してたら ssh: connect to host 192.0.2.10 port 22: … | polarity | loss/loss/worsening | loss/loss/none |
| u009 | 昨日、あなたに教えてもらった方法で確定申告の書類を作ったんだけど、控除の欄の書き方が間違っていたみたいで、税務署の窓口で… | core6,polarity | criticism/none/none | criticism/loss/worsening |
| u010 | お世話になっております。弊社の社内システムにて、身に覚えのない外部IPからの不正ログインが複数確認されました。現在進行形… | polarity | threat/threat/none | threat/threat/worsening |
| u011 | 先週から毎朝、家を出る直前になって洗濯機のエラーが出て止まるんです。そのたびにやり直して遅刻ギリギリになるし、説明書の通… | polarity | frustration/threat/none | frustration/threat/worsening |
| u012 | 先週教えていただいた方法で、無事に家計簿アプリの集計ができました。とても分かりやすかったです。ありがとうございます。 | label,core6,polarity | achievement/achievement/none | connection/connection/resolving |
| u014 | テスト全部やり直したのにまた落ちた…！！😭💦 (´；ω；｀) 3件も赤くなってる〜〜〜 | core6 | failure/threat/none | failure/none/none |
| u015 | さっきデプロイを実行したのですが、途中でエラーが出て止まってしまいました。原因はまだわかっていません。 | core6 | failure/threat/none | failure/uncertainty/none |
| u016 | Pythonでこのエラーが出ました。原因と直し方を教えてください。  Traceback (most recent ca… | label,core6 | failure/threat/none | none/none/none |
| u017 | ちょっと前に買った観葉植物の葉っぱの先が茶色くなってきているんですけど、水やりの頻度ってどのくらいが目安になりますかね？ | label,core6,polarity | threat/threat/worsening | none/none/none |
| u018 | ずっと気がかりだった例の件ですが、今朝ようやく肩の荷が下りました。あとは結果を待つだけで、ひと安心という感じです。 | core6 | threat_resolved/joy/resolving | threat_resolved/none/resolving |
| u019 | wwwwwwww手が滑ったｗｗｗ「えおいうあqqqqq」とか草すぎて送信ボタン押した自分にワロタ、つらたん | label,core6 | joy/joy/none | none/none/none |
| u022 | 助かったよ、ありがとう | polarity | connection/connection/resolving | connection/connection/none |
| u023 | 長年やってきたチームがreorgで解散になって、メンバーともほぼ会えなくなった。Slackのチャンネルもarchiveさ… | polarity | loss/loss/worsening | loss/loss/none |
| u038 | ビルド落ちた | polarity | failure/threat/none | failure/threat/worsening |
| u042 | Traceback (most recent call last):   File "<stdin>", line 1,… | label,core6 | failure/threat/none | none/none/none |
| u046 | やったーーー！！🎉🎉 資格試験、合格したよ〜！(≧▽≦) 3ヶ月がんばった甲斐あった✨ ありがとう〜！💪😭 | polarity | achievement/achievement/none | achievement/achievement/resolving |
| u047 | ねえ聞いて、ついにマラソン完走したよ！タイムは遅かったけど、最後まで歩かずに走れたんだ。めっちゃ嬉しい！ | polarity | achievement/achievement/none | achievement/achievement/resolving |
| u056 | 教えてもらった手順で一通りやってみたんですが、最後のところで画面が思っていたのと違う感じになって、結局完了までは行けませ… | core6 | failure/threat/none | failure/uncertainty/none |
| u061 | さっきの停電、やっと復旧したわ。冷蔵庫の中身ダメになるかと思ったけど何とかセーフだった | core6 | threat_resolved/threat/resolving | threat_resolved/joy/resolving |
| u063 | 終わった！ | polarity | achievement/achievement/none | achievement/achievement/resolving |
| u067 | ビルド通らんの何回目だよw 直したと思ったらまた別のとこで落ちるの草通り越してつらたん | label,core6,polarity | frustration/threat/none | failure/none/worsening |
| u072 | ついにレポ提出完了したwwwwww 徹夜明けでテンションおかしいけど草。もう二度とギリギリまで放置しないマジで | polarity | achievement/achievement/none | achievement/achievement/resolving |
| u088 | CI のパイプラインが途中でfailしちゃって、Dockerのimage buildのステップでtimeoutしてるみた… | core6 | failure/threat/none | failure/none/none |
| u089 | npm install するたびにこれ出るんだけど、もう何回目か分からない。  npm ERR! code ERESOL… | polarity | frustration/threat/none | frustration/threat/worsening |
| u093 | さっきのSQLのクエリ、indexの貼り方がそもそも間違ってない？ 実行したらfull scanになってパフォーマンスが… | core6 | criticism/none/worsening | criticism/threat/worsening |
| u095 | npm run build したらこうなった： Error: Cannot find module './config'… | core6 | failure/threat/none | failure/none/none |
| u103 | 前のあなたは、私の話にもっと寄り添って頷いてくれていた気がするんです。今日はなんだか冷たいですね。昨日みたいに、私の考え… | core6 | pressure/none/none | pressure/connection/none |
| u113 | また落ちた… | polarity | failure/none/none | failure/none/worsening |
| u115 | すみません、さっきから何度も送信しようとしているのですが、変な文字になってしまいます。「ぬおdfghjkl」のような文字… | core6 | failure/threat/none | failure/none/none |
| u124 | 今日だけで三回もWi-Fiが切れて、オンライン会議が中断されました。毎回つなぎ直すのも疲れてきたので、原因の調べ方を教え… | core6,polarity | frustration/threat/none | frustration/none/worsening |
| u127 | またゲームのセーブデータ飛んだんだけど。これで3回目だよ？もういい加減にしてほしいわ、まじで。バックアップの取り方教えて | polarity | frustration/loss/none | frustration/loss/worsening |
| u134 | さっき自分のポートフォリオサイトをVercelにデプロイできた！Next.jsで書いてCI/CDも設定したから、push… | polarity | achievement/achievement/none | achievement/achievement/resolving |
| u136 | もおおおお！！😡💢 また同じミスで怒られた…(´;ω;｀) 毎回毎回なんなの〜！！！ 😤 もう嫌だぁぁ🔥 | core6,polarity | frustration/none/none | frustration/threat/worsening |
| u140 | お世話になっております。同一の請求書の差し戻しが今月だけで四度目となり、正直なところ対応に疲弊しております。差し戻しを防… | core6,polarity | frustration/none/none | frustration/uncertainty/worsening |

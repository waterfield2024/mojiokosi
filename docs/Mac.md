# Apple Silicon Macで使う

M1・M2・M3・M4などのMシリーズ、macOS 14 Sonoma以降、20GB以上の空き容量が必要です。
Whisper baseから始めてください。turbo・Granite・文脈AIを併用する場合は16GB以上のメモリを推奨します。

## 初回の準備と起動

1. [Homebrew公式](https://brew.sh/ja/)の手順でHomebrewを導入します。すでに入っていれば不要です。
2. GitHubからこのフォルダーを取得します。
3. Terminalで次を実行します。以降は `start.command` をダブルクリックして起動できます。

```bash
git clone https://github.com/waterfield2024/mojiokosi.git
cd mojiokosi
bash start.command
```

ZIPで取得した場合も、展開先にTerminalで移動して `bash start.command` を実行できます。
Finderで実行権限がないと言われた場合は `chmod +x start.command setup.command` を一度実行してください。
Rosetta経由では動かさず、Terminalの「Rosettaを使用して開く」はオフにします。

スクリプトがHomebrewのPython 3.12・Tk・[字幕対応FFmpeg](https://formulae.brew.sh/formula/ffmpeg-full)・Node.js・Ollamaを確認し、不足分を導入します。
その後、このフォルダーの `.venv-macos` にMac用ライブラリ、`models` に認識・話者・無音判定のモデルを準備します。
文脈AIのモデルも初回に取得します（約5GB）。数分以上かかる場合があります。
Homebrewのサービス起動や、Macの音声出力先・権限の変更は自動で行いません。

AI校正モデルのダウンロードを省く場合は `bash setup.command --without-context-ai`。
後から追加するときは `bash setup.command`。基本の文章連結はAIなしでも使えます。
CPUで動かしたい場合は `bash setup.command --device cpu`、自動に戻す場合は `--device auto` を指定します。
起動後の「処理方式」でも切り替えられます。

## モデルと処理方式

- Whisper（base／small／turbo）とGraniteはApple GPUのMetal（MPS）を優先します。初期化や認識で失敗した場合はCPUで再試行します。
- Haas-ja-v1.0と話者判別はCPUです。話者名の記憶、辞書、比較、プレビュー、MP4字幕保存も同じ画面から操作できます。
- 文脈AIはこのアプリ専用のOllamaプロセスを使い、通常のOllamaサーバーやモデル保存先は変更しません。
- NemotronLabs-RE-USEはNVIDIA CUDAを必要とするため、Macでは選択できません。
- GPUが使える場合も、速度はチップ・メモリ・モデル・文章の長さによって変わります。

[AppleのPyTorch／Metal要件](https://developer.apple.com/metal/pytorch/)に基づく構成です。
録音・画面収録とGPU負荷のある長時間利用は、使用するMacで確認してください。

## Macで再生中の動画をリアルタイム文字起こし

Windowsの音声ループバックに相当する経路として、[BlackHole公式](https://github.com/ExistentialAudio/BlackHole)の2ch版を使います。
**Macのマイクへ自動で切り替わることはありません。**

1. Terminalで `brew install --cask blackhole-2ch` を実行します。インストーラーが管理者パスワードを求める場合があります。
2. 「Audio MIDI設定」を開き、左下の「＋」→「複数出力装置を作成」を選びます。
3. MacBookのスピーカー（またはヘッドホン）と「BlackHole 2ch」を有効にします。スピーカー側をクロックソースにし、BlackHole側にドリフト補正を付けます。
4. Macの「システム設定 → サウンド → 出力」をこの複数出力装置にします。
5. アプリの「PC音声をリアルタイム…」で「BlackHole 2ch」を選び、リアルタイム開始を押します。

音声入力の許可を求められたら、起動に使ったTerminal／Pythonを許可してください。
設定が見つからない場合は「システム設定 → プライバシーとセキュリティ → マイク」を確認します。
この許可名は「マイク」でも、アプリが開く音声デバイスは選択したBlackHoleです。
聞こえない／無音になる場合は、[BlackHole公式の複数出力手順](https://github.com/ExistentialAudio/BlackHole/wiki/Getting-Started%3A-Creating-a-Multi-Output-Device)も参照してください。

録画する場合は「画面全体」または「ウィンドウ」を選びます。Macの「画面収録」の許可が必要です。
ScreenCaptureKitで選んだ対象を取得し、画面全体の録画ではWhisper自身の字幕・履歴を除外します。
保護された動画は録画できない場合があります。停止後に字幕付きMP4を保存します。
停止・再開のショートカットは `Control + Option + F9`（Macのキー設定によっては `fn` も押します）。

## データを引き継ぐ

設定・覚えた話者・学習音声・辞書・録音は、Windowsと同じ `UserData` に保存します。
Windowsからフォルダーごとコピーしても、MacではWindowsの `.venv` やEXEを実行せず、`.venv-macos` を作ります。
モデルは共用します。別PCで使っていた録画ウィンドウ・音声デバイスは、Macのものを選び直してください。
元動画がフォルダー外にある結果は、元動画もコピーしてプレビューで選び直してください。
アプリ内のGitHub更新とバックアップ／復元も利用でき、`UserData` とモデルは保持します。

ログは `.cache/setup.log` と各結果フォルダーにあります。再セットアップは `bash setup.command` です。

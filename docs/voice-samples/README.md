# Azure 主席聲線提案

這兩段是以相同測試話術和 `+12%` 語速產生的 Azure Speech 合成音，
供 PR 審查者比較主席聲線；不包含真人錄音、私人會議內容或 API key。

- [1 號：小辰女聲](01-hsiaochen-female.wav) — `AZURE_TTS_GENDER=female`
- [5 號：雲哲男聲](05-yunjhe-male.wav) — `AZURE_TTS_GENDER=male`

兩個預設都保留已確認的語速與中英文發音處理。實際執行時仍由
`.env` 選擇聲線，音檔不會被程式當成會議輸入。

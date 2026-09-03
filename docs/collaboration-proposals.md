# 可向 Ahem 上游提交的 Issue／PR 提案

建議拆成小型、可獨立審查的變更，不一次提交大型安全重構：

1. **PR：移除 Discord voice secret 與封包日誌**
   附回歸測試，證明 CryptoError 只留下封包長度。
2. **PR：Spectator loopback 與 Viewer／Operator Token**
   說明相容性變更、HTTP 401/403 測試與安全標頭。
3. **PR：私有檔案權限與 Envelope Encryption**
   說明 Keychain/KMS 介面、AEAD 參數、Nonce 及竄改測試。
4. **PR：同意閘門與保存期限**
   附隱私告知、fail-closed 行為與 dry-run 清除工具。
5. **PR：CI Security Gates**
   加入 pytest、Bandit、pip-audit 及 CycloneDX SBOM。

安全漏洞細節應先依 `SECURITY.md` 私密回報；在維護者修補或同意公開前，不建立含
可利用細節的公開 Issue。

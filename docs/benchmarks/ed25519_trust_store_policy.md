# AI Coordinator — Política de Governança Criptográfica, Ed25519 e Trust Store (Fase 4C.2.2)

**Documento:** Política de Governança Criptográfica, Ciclo de Vida de Chaves e Trust Store  
**Versão:** 1.0.0  
**Data:** 2026-11-09  
**Fase:** 4C.2.2  
**Módulo:** `ai_coordinator.manifest_auth`  
**Status:** Implementado e Validado Offline  
**Repositório:** `D:\ai-coordinator`  
**Commit Base:** `1a0adb93ad5c4ebe755beb3d6b07dccfc43eeb92` (Preservado e Congelado)  
**Branch:** `feature/phase4c-2-ed25519-truststore`  

---

## 1. Sumário e Objetivos de Segurança

A Fase 4C.2.2 implementa a camada de **Autorização Criptograficamente Verificável** do AI Coordinator, substituindo manifestos baseados exclusivamente em JSON não autenticado por assinaturas digitais assimétricas baseadas no padrão **Ed25519 (RFC 8032)**.

O objetivo fundamental é garantir que **nenhuma tentativa lógica, reserva de orçamento ou despacho físico** para provedores externos (OpenRouter) seja executado sem a comprovação simultânea de:
1. **Integridade absoluta** dos parâmetros de execução (modelo, modalidade, orçamento US$ 0.00 e commit exato);
2. **Autoridade do emissor** devidamente cadastrado e ativo em uma Trust Store local governada;
3. **Consentimento operacional explícito do operador humano** na linha de comando ou automação de voo.

---

## 2. Padrões Criptográficos Adotados

| Parâmetro | Especificação | Justificativa de Engenharia |
| :--- | :--- | :--- |
| **Algoritmo de Assinatura** | PureEdDSA (Ed25519) | RFC 8032. Alta performance, imunidade a ataques de canal lateral baseados em tempo e ausência de nonces aleatórios voláteis (determinístico). |
| **Curva Elíptica** | Curve25519 ($y^2 = x^3 + 486662x^2 + x$) | Fornece ~128 bits de nível de segurança com chaves compactas de 256 bits (32 bytes). |
| **Função de Hash Interna** | SHA-512 | Padrão estrito do RFC 8032 §5.1.6 para expansão de chave e redução modular. |
| **Tamanho da Chave Pública** | 32 bytes (64 caracteres hexadecimais) | Compacta, permite embedding em cabeçalhos de auditoria sem overhead. |
| **Tamanho da Assinatura** | 64 bytes (128 caracteres hexadecimais) | Determinística; não varia de tamanho com o payload. |
| **Biblioteca de Criptografia** | `cryptography.hazmat.primitives.asymmetric.ed25519` | Biblioteca amplamente auditada pela comunidade internacional, eliminando implementações criptográficas proprietárias. |

---

## 3. Especificação do Manifesto Canônico e Serialização Determinística

Para prevenir ataques de formatação, espaços em branco ou ambiguidade de tipos (como diferenças entre interpretadores JSON em Node, Python e Go), o AI Coordinator adota uma **Representação Canônica Determinística** (subconjunto formal do RFC 8785 — JSON Canonicalization Scheme):

### 3.1 Campos Obrigatórios do Esquema 1.0

Todo manifesto deve conter estritamente os 11 campos a seguir (campos adicionais não homologados são rejeitados fail-closed para evitar shadow-injections):

```json
{
  "allowed_modalities": ["text"],
  "allowed_models": [
    "liquid/lfm-3-1.7b:free",
    "meta-llama/llama-3.2-3b-instruct:free"
  ],
  "authorization_id": "auth-20261109-001",
  "authorizer_identity": "security_operator_root",
  "authorizer_permissions": [
    "dispatch:openrouter_free"
  ],
  "code_commit": "1a0adb93ad5c4ebe755beb3d6b07dccfc43eeb92",
  "execution_scope_id": "pilot_n1_20261109",
  "issued_at_utc": "2026-11-09T17:00:00Z",
  "max_budget_usd": 0.0,
  "max_dispatches": 1,
  "schema_version": "1.0",
  "valid_until_utc": "2026-11-10T23:59:59Z"
}
```

### 3.2 Regras de Canonicidade de Bytes
1. **Ordenação Lexicográfica**: As chaves do objeto JSON são ordenadas estritamente em ordem crescente por seus pontos de código Unicode/ASCII (`sort_keys=True`).
2. **Separadores Mínimos**: Eliminação total de espaços em branco entre tokens (`separators=(',', ':')`).
3. **Codificação UTF-8**: Caracteres representados diretamente em UTF-8 sem escape hexadecimal (`ensure_ascii=False`).
4. **Determinismo Numérico**: `max_budget_usd` deve ser formatado estritamente como `0.0`. Qualquer valor diferente de zero invalida imediatamente a construção do manifesto.
5. **Preservação de Sequência de Modelos**: O array `allowed_models` preserva rigorosamente a ordem declarada, que define a cadeia de prioridade do fallback multi-modelo.

A assinatura Ed25519 é gerada diretamente sobre os bytes UTF-8 resultantes dessa serialização canônica (`to_canonical_bytes()`).

---

## 4. Separação Tripartite de Autorização

O AI Coordinator desacopla rigorosamente a verificação em **três camadas ortogonais**:

```
+---------------------------------------------------------------------------------------------------+
|                                 ARQUITETURA DE AUTORIZAÇÃO TRIPARTITE                             |
+---------------------------------------------------------------------------------------------------+
|                                                                                                   |
|  [TIER 1: VALIDADE CRIPTOGRÁFICA]                                                                 |
|  - A assinatura Ed25519 é matematicamente válida sobre os bytes canônicos do manifesto?           |
|  - Houve qualquer alteração de bits, truncamento ou adulteração de campos após a assinatura?      |
|  => SIM: Prossegue para Tier 2.                                                                   |
|  => NÃO: Rejeição imediata (CryptographicSignatureError).                                         |
|                                                                                                   |
|                                         |                                                         |
|                                         v (Apenas se Tier 1 for VÁLIDO)                           |
|                                                                                                   |
|  [TIER 2: AUTORIDADE DA CHAVE NA TRUST STORE]                                                     |
|  - O key_id está cadastrado na Trust Store local?                                                 |
|  - A chave está com status 'active' (NÃO revogada e NÃO suspensa)?                                |
|  - A chave está dentro de sua validade temporal (expires_at_utc)?                                 |
|  - A chave pertence à entidade declarada (authorizer_identity)?                                   |
|  - A chave possui a permissão necessária (ex: dispatch:openrouter_free)?                          |
|  => SIM: Prossegue para Tier 3.                                                                   |
|  => NÃO: Rejeição imediata (KeyAuthorityError).                                                   |
|                                                                                                   |
|                                         |                                                         |
|                                         v (Apenas se Tier 2 for AUTORIZADO)                       |
|                                                                                                   |
|  [TIER 3: AUTORIZAÇÃO OPERACIONAL HUMANA E ESCOPO DE EXECUÇÃO]                                    |
|  - O code_commit assinado bate exatamente com o commit atual do repositório (git HEAD)?           |
|  - O orçamento assinado é estritamente US$ 0.00?                                                  |
|  - A janela de validade do manifesto (valid_until_utc) ainda está vigente?                        |
|  - O modelo solicitado para despacho está na lista allowed_models?                                |
|  - A modalidade solicitada está na lista allowed_modalities?                                      |
|  - O total de despachos solicitados <= max_dispatches?                                            |
|  - O operador humano forneceu o consentimento explícito (--authorized-by-user)?                   |
|  - O authorization_id é inédito (prevenção fail-closed contra replay attack)?                     |
|  => SIM: DESPACHO AUTORIZADO (is_fully_authorized = True).                                        |
|  => NÃO: Rejeição imediata (HumanOperationalAuthorizationError / ReplayAuthorizationError).       |
|                                                                                                   |
+---------------------------------------------------------------------------------------------------+
```

Essa separação garante que:
- Uma chave válida jamais pode autorizar ações fora de suas permissões específicas;
- Um invasor que possua uma chave privada de testes não consegue autorizar nada, pois a chave não consta na Trust Store (Tier 2 falha);
- Um operador autorizado com manifesto válido não consegue despachar código adulterado se o commit divergir do HEAD (Tier 3 falha);
- Uma automação não pode disparar inferências sem o consentimento operacional humano explícito (Tier 3 falha).

---

## 5. Governança e Ciclo de Vida da Trust Store

A **Trust Store** (`TrustStore`) armazena **exclusivamente chaves públicas** e metadados de governança.

### 5.1 Invariante de Segurança P0: Proibição Estrita de Chaves Privadas
A Trust Store rejeita a inclusão ou persistência de qualquer material de chave privada (`private_key`, `private_bytes`, `secret`). Qualquer tentativa de passagem de chaves privadas aciona exceção de segurança imediata (`ValueError`). As chaves privadas devem permanecer exclusivamente em custódia do operador (armazenadas em módulos seguros ou variáveis protegidas offline).

### 5.2 Formato de Armazenamento da Trust Store (`trust_store.json`)

```json
{
  "version": "1.0",
  "updated_at_utc": "2026-11-09T17:15:00Z",
  "keys": [
    {
      "key_id": "ed25519:operator_primary_2026",
      "public_key_hex": "b3e0c0a9...64_hex_chars...1a",
      "identity": "security_operator_root",
      "permissions": [
        "dispatch:openrouter_free",
        "audit:verify"
      ],
      "status": "active",
      "created_at_utc": "2026-11-09T17:00:00Z",
      "expires_at_utc": "2027-11-09T17:00:00Z",
      "revoked_at_utc": null,
      "revocation_reason": null
    }
  ]
}
```

### 5.3 Estados de uma Chave (`status`)
1. **`active`**: Chave autorizada e apta a assinar manifestos dentro de suas permissões e validade.
2. **`revoked`**: Chave revogada em caráter definitivo. Não pode ser reativada. Requer preenchimento obrigatório de `revoked_at_utc` e `revocation_reason`.
3. **`suspended`**: Chave temporariamente inativa para averiguação de segurança. Rejeita autorizações até manifestação expressa.

---

## 6. Procedimentos Operacionais: Rotação e Revogação de Chaves

### 6.1 Procedimento de Rotação Periódica de Chaves
Chaves operacionais devem ser rotacionadas a cada 90 ou 365 dias, conforme criticidade do ambiente. O método `TrustStore.rotate_key` executa a rotação de forma atômica:
1. Gera-se o novo par de chaves em ambiente seguro offline;
2. Instancia-se o novo registro `TrustedKeyEntry` com status `active`;
3. Executa-se `trust_store.rotate_key(old_key_id, new_entry, reason)`;
4. A chave antiga é imediatamente marcada como `revoked` com a razão `"Rotated: <motivo>. Replaced by <novo_id>"`;
5. A nova chave é registrada e a Trust Store é persistida em disco com `os.fsync()`.

### 6.2 Procedimento de Revogação Emergencial
Em caso de suspeita ou confirmação de vazamento, comprometimento de estação ou desligamento de operador:
1. O administrador executa a revogação imediatamente:
   ```python
   trust_store.revoke_key(
       key_id="ed25519:compromised_key_id",
       reason="Comprometimento operacional detectado em auditoria de segurança",
   )
   trust_store.save_to_file()
   ```
2. O status é alterado para `revoked` com data/hora e justificativa registradas;
3. Qualquer manifesto pré-assinado por essa chave que tente ser utilizado após o momento da revogação será **imediatamente rejeitado no Tier 2** em modo fail-closed, impedindo qualquer reserva de orçamento ou despacho físico.

---

## 7. Taxonomia de Permissões Homologadas

| Permissão | Escopo de Ação Autorizado |
| :--- | :--- |
| `dispatch:openrouter_free` | Autoriza reserva de orçamento e despacho físico para modelos gratuitos certificados no catálogo oficial. |
| `audit:verify` | Autoriza consulta e validação independente de relatórios científicos de baseline e trilhas de auditoria. |
| `catalog:sync` | Autoriza sincronização e assinatura de snapshots locais do catálogo de modelos da OpenRouter. |
| `emergency:override` | Permissão restrita ao operador sênior para drenagem e sanitização manual de registros em estado `UNKNOWN`. |

---

## 8. Proteção Contra Ataques de Replay e Idempotência

O AI Coordinator garante proteção contra reutilização maliciosa ou acidental de manifestos assinados:
1. Cada manifesto possui um identificador único universal (`authorization_id`, UUIDv4 ou nonce estruturado).
2. O verificador tripartite mantém um cache persistente/volátil de identificadores consumidos (`_replay_cache`).
3. Uma vez completada com sucesso a autorização de um `authorization_id`, tentativas subsequentes de apresentação do mesmo manifesto para novo despacho são sumariamente barradas no Tier 3 com `ReplayAuthorizationError`.
4. Para execuções em lote ou planos de voo multi-etapa, utiliza-se o campo `execution_scope_id` vinculado a um limite estrito em `max_dispatches`.

---

## 9. Conformidade com os Mandatos de Segurança

- [x] **Zero Dependências Obscuras**: Utilização exclusiva de `cryptography` (Ed25519) padrão da indústria.
- [x] **Zero Rede Externa**: Todos os testes e algoritmos operam 100% offline.
- [x] **Desacoplamento Tripartite Rigoroso**: Validade Criptográfica $\ne$ Autoridade da Chave $\ne$ Autorização Operacional Humana.
- [x] **Prevenção At-Most-Once**: O manifesto assinado é validado antes de qualquer reserva no SQLite e antes do ponto de compromisso irreversível (`SEND_COMMITTED`).
- [x] **Branch Main Preservada**: Branch `main` permanece intacta em `a2f8f65` (`v0.5.1`).

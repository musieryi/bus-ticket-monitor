# GitHub Actions 部署手册

> 目标：让余票监控在 GitHub 的服务器上每 5 分钟自动跑一次，**完全免费、不用管电脑开没开**。
> 全程约 15 分钟，不需要懂服务器运维。

---

## 一、这个目录里有什么

| 文件 | 作用 |
|---|---|
| `monitor.py` | 监控核心（查询 / 判断 / 推送） |
| `refresh_token.py` | token 刷新模块（被 monitor 引用） |
| `requirements.txt` | 依赖清单（requests、pycryptodome） |
| `.github/workflows/monitor.yml` | 定时任务配置（**核心**） |
| `.gitignore` | 防止密钥被误提交 |

**这些文件要上传到你的 GitHub 仓库。** 注意 `.github` 是隐藏目录，别漏掉。

---

## 二、第一步：创建 GitHub 仓库

1. 登录 [github.com](https://github.com)，右上角 `+` → **New repository**
2. 填写：
   - Repository name：随意，如 `bus-ticket-monitor`
   - 可见性：**Public（推荐）** ← 公开仓库的 Actions 完全免费不计分钟数
   - 不要勾选 "Add a README file"（保持空仓库）
3. 点 **Create repository**

> **为什么推荐公开仓库？**
> 公开仓库的 Actions 分钟数**无限且免费**，可以每 5 分钟跑一次。
> 私有仓库每月只有 2000 分钟，每 5 分钟跑会超（约需 4300 分钟），只能降到每 15 分钟。
> 仓库里只有代码，**密钥不会上传**（用 Secrets 存储），公开没有安全风险。

---

## 三、第二步：上传代码

### 方法 A：网页上传（最简单，适合不熟悉 git）

1. 进入刚创建的仓库，点 **Add file** → **Upload files**
2. 把 `monitor.py`、`refresh_token.py`、`requirements.txt` 拖进页面
3. 点 **Commit changes**

4. **再上传 workflow 文件**（这个必须手动建目录）：
   - 点 **Add file** → **Create new file**
   - 文件名输入框里直接打：`.github/workflows/monitor.yml`
     （输入 `/` 会自动创建目录层级）
   - 把本目录下 `.github/workflows/monitor.yml` 的内容整个粘进去
   - 点 **Commit changes**

### 方法 B：命令行（如果你装了 git）

```bash
cd 云端部署/github-actions
git init
git add .
git commit -m "init: 余票监控"
git branch -M main
git remote add origin https://github.com/你的用户名/bus-ticket-monitor.git
git push -u origin main
```

---

## 四、第三步：配置密钥（关键，别搞错）

代码需要 `config.json`（含车次参数、推送 token）和 `private_key.pem`（签名私钥），
但**这两个文件绝不能提交到仓库**。改用 GitHub Secrets 注入。

### 4.1 添加两个 Secret

1. 进入仓库 → **Settings** → 左侧 **Secrets and variables** → **Actions**
2. 点 **New repository secret**，添加第一个：
   - Name：`CONFIG_JSON`
   - Secret：用记事本打开本地 `config.json`，**全选复制全部内容**，粘进来
   - 点 **Add secret**
3. 再点 **New repository secret**，添加第二个：
   - Name：`PRIVATE_KEY_PEM`
   - Secret：打开 `private_key.pem`，**包含 `-----BEGIN...` 和 `-----END...` 两行**全部复制
   - 点 **Add secret**

> ⚠️ 复制时注意：不要漏掉开头结尾的 `-----BEGIN RSA PRIVATE KEY-----` / `-----END RSA PRIVATE KEY-----`。
> 粘贴后末尾不要留多余空行（一般不影响，脚本用的是 `printf '%s'`）。

### 4.2 开启写权限（否则状态存不下来）

1. 仓库 **Settings** → 左侧 **Actions** → **General**
2. 滚到最下面 **Workflow permissions**
3. 选 **Read and write permissions**
4. 点 **Save**

> **为什么需要写权限？**
> 程序要把「今天已经提醒过几次」存到 `state.db` 并提交回仓库。
> 没有写权限，每轮都会当成新的一天，导致**每 5 分钟推一条，一天 288 条刷屏**。

---

## 五、第四步：先手动测一次

别急着等定时触发，先手动跑一次确认通路。

1. 进入仓库 → 顶部 **Actions** 标签
2. 左侧选 **余票监控**
3. 右边有提示 "This workflow has a workflow_dispatch event trigger" → 点 **Run workflow** → 绿色按钮
4. 等约 30 秒，刷新页面，点进这次运行记录
5. 展开 **查询余票并推送** 步骤，看日志：

正常应该看到：
```
[080101] 后 3 个工作日：2026-11-27=0/41，2026-11-30=0/41，2026-12-01=0/41
[080801] 后 3 个工作日：2026-11-27=1/42，2026-11-30=1/42，2026-12-01=2/42
推送成功（渠道: pushplus，仅自己）
::MONITOR_RESULT alerted=true ok=true
```

**同时检查微信**：如果有票，你应该收到一条 PushPlus 推送。收到了就说明全通了。

### 常见报错对照

| 报错 | 原因 | 解决 |
|---|---|---|
| `缺少 Secret: CONFIG_JSON` | Secret 名字拼错或没加 | 回第四步检查，名字必须完全一致 |
| `config.json 校验通过` 之后报 JSON 错误 | 粘贴时截断或多了字符 | 重新复制完整内容 |
| `登录失效（token 已过期）` | token 失效 | 见下方「token 过期怎么办」 |
| `Permission denied` / `403` on push | 没开写权限 | 回 4.2 开启 Read and write |
| `所有推送渠道均失败` | 推送 token 失效 | 检查 config.json 里的 pushplus_token |

---

## 六、之后它就会自己跑了

- 每 5 分钟自动执行一次，你什么都不用做
- 有票时推送微信，没票就静默
- 手机、电脑关机都不影响（跑在 GitHub 服务器上）

### 想改频率？

编辑 `.github/workflows/monitor.yml` 里的 cron：

```yaml
schedule:
  - cron: "*/5 * * * *"     # 每 5 分钟（公开仓库推荐）
  # - cron: "*/15 * * * *"  # 每 15 分钟（私有仓库用这个）
  # - cron: "*/10 * * * *"  # 每 10 分钟
```

> GitHub 的 cron **最小间隔就是 5 分钟**，写 `* * * * *` 不会每分钟跑。
> 另外平台繁忙时可能延迟几分钟，这正常，余票监控不差这几分钟。

---

## 七、几个必须知道的坑

### 坑 1：公开仓库 60 天无活动会停用定时任务

GitHub 规定：**公开仓库连续 60 天没有任何提交，定时工作流会被自动禁用**。

解决办法（任选）：
- 偶尔去 Actions 页面点一下 **Run workflow**
- 或者随便改一下 README 提交
- 被停用时 GitHub 会发邮件通知你

> 注意：本项目推送成功时会自动提交 `state.db`，这本身就是"仓库活动"。
> 所以只要偶尔有票推送，就不会触发 60 天规则。长期无票才需要手动点一下。

### 坑 2：token 过期（这个必须你手动处理）

登录态靠短信验证码换来，**没法在云端无人值守地自动续期**（这是接口的安全设计，不是程序缺陷）。

token 通常能撑**数天到数周**。过期后：
1. 程序日志报「登录失效」，微信会推一条告警（**每天只推 1 条**，不会刷屏）
2. 你在**本地 Windows** 上打开 `定制公交余票监控.exe`
3. 点「刷新登录」→ 输手机号 → 收验证码 → 登录
4. 本地的 `config.json` 会被自动更新
5. 打开新的 `config.json`，复制全部内容
6. 回 GitHub 仓库 → Settings → Secrets → 点 `CONFIG_JSON` → **Update** → 粘贴新内容 → 保存

下次运行就自动生效了。

> 提示：token 有效期没法提前知道，建议收到告警就顺手更新一下，一分钟的事。

### 坑 3：不要提交 config.json 和 private_key.pem

`.gitignore` 已经排除了这两个文件。如果你用网页上传，**不要手动上传它们**。
私钥泄露意味着别人能伪造你的请求签名。

### 坑 4：state.db 需要提交，别加进 .gitignore

`state.db` 是「今天已提醒次数 / 票情指纹 / 告警去重日期」的载体。
workflow 会在**推送成功时**把它提交回仓库（用 `[skip ci]` 标记避免触发新的 workflow）。

- 提交次数 = 实际推送次数（一天最多几条），不会污染仓库历史
- 如果把它误加进 .gitignore，会导致重复推送

---

## 八、状态持久化是怎么做的（想改的人看）

这是本方案最需要小心的地方，说明一下设计：

```
每次运行：
  1. checkout 仓库（自带上一轮提交的 state.db）
  2. 写 config.json / private_key.pem（从 Secrets）
  3. python monitor.py --once
       ├─ 读 state.db 判断今日是否已提醒
       ├─ 有票 → 推送 → 写 state.db
       └─ 输出 ::MONITOR_RESULT alerted=true/false
  4. 删除密钥文件
  5. 仅当 alerted=true → git commit & push state.db
```

**为什么不每轮都提交？** 一天 288 次提交会把仓库历史撑爆。
**为什么只在不推送时提交就不行？** 因为不提交就丢了「今天已推过」的记录，会重复推。

所以：**只在真正推送后提交**。最坏情况（首次部署、state.db 还没建立）同一天可能多推一两次，
但绝不会无限刷屏——因为第一次推送成功后状态就被保存了。

---

## 九、成本确认

| 项目 | 用量 | 费用 |
|---|---|---|
| Actions 分钟数（公开仓库） | 无限 | **￥0** |
| Actions 分钟数（私有仓库） | 每 15 分钟约 1440 分钟/月 | 免费额度 2000 分钟内 |
| 仓库存储 | state.db 12 KB | **￥0** |
| PushPlus 推送 | 免费额度 200 条/天 | **￥0** |

**结论：公开仓库方案长期 0 成本。**

---

## 十、验证清单

部署完对照打勾：

- [ ] 仓库里有 `monitor.py`、`refresh_token.py`、`requirements.txt`
- [ ] 有 `.github/workflows/monitor.yml`（注意 `.github` 开头的点）
- [ ] Secrets 里有 `CONFIG_JSON` 和 `PRIVATE_KEY_PEM`
- [ ] Workflow permissions 设为 Read and write
- [ ] 手动 Run workflow 跑成功，日志有 `::MONITOR_RESULT`
- [ ] 微信收到了推送（有票的情况下）
- [ ] `config.json`、`private_key.pem` **不在**仓库文件列表里
- [ ] 仓库里出现了 `state.db`（说明状态持久化生效）

全打勾就完成了。

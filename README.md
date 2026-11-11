# 内部图床（imghost）

单文件、零依赖、免登录的内网图床。浏览器里把图片拖进去，点「复制链接」就能贴到任何地方。

只依赖 Python 标准库，**不需要 pip install、不需要 Docker、不需要数据库、不需要证书**。

---

## 快速开始

双击 **`启动图床.cmd`**，浏览器会自动打开 `http://127.0.0.1:8899`。

- 把图片拖到网页虚线框里 → 上传完成
- 每张图下面点 **复制链接** → 拿到 `http://192.168.67.24:8899/图片名.png`
- 把这个链接贴到文档、Confluence、内部系统、聊天窗口都能直接显示

同事访问就用启动时打印的局域网地址：

```
http://192.168.67.24:8899
```

如果打不开，用**管理员** PowerShell 执行一次（只需一次）：

```powershell
netsh advfirewall firewall add rule name="imghost 8899" dir=in action=allow protocol=TCP localport=8899
```

## 在 React / UE 里用（推荐：同源）

如果你在 React 页面里 `<img src="...">` 引图，或者用 **UE 的 `SWebBrowser`** 加载界面，
**最好的做法是让图片和页面同源**——直接用相对路径，不写 IP 和端口。

启动时把 `--cdn-base` 配成一个相对前缀即可：

```bat
rem 图片真实存放在 React 项目的 public\imgs 下，对外路径是 /imgs
python imghost.py --port 8899 --dir "D:\my-react\public\imgs" --cdn-base /imgs
```

页面里就这样写：

```jsx
<img src="/imgs/物件说明.png" />
```

「复制链接」复制出来的也是 `/imgs/物件说明.png` 这种相对地址。好处：
- **不会触发混合内容拦截**——即使页面本身是 https，同源相对路径也照常显示
- **换机器、换 IP、换端口都不用改代码**
- 不需要装证书、不需要 HTTPS

### Vite 开发环境

```js
// vite.config.js
export default {
  server: {
    proxy: { '/imgs': 'http://127.0.0.1:8899' },
  },
};
```

不用写 `rewrite`——imghost 只取路径最后一段当文件名，
`/imgs/x.png`、`/a/b/x.png`、`/任意前缀/x.png` 都能拿到同一张图。

### 打包

`public` 目录下的文件会被 Vite 原样拷进 `dist`，所以直接让图床指向 `public/imgs` 即可，
开发和线上路径完全一致。如果图片不在 `public` 下，构建后补一条：

```bat
xcopy /E /I /Y "D:\图片库" "D:\my-react\dist\imgs"
```

### 如果页面本身也是 http

那最简单：直接用绝对地址 `http://192.168.67.24:8899/图.png`，什么都不用配。

## 常用参数

```bat
python imghost.py ^
  --port 8899 ^
  --dir D:\图片库 ^
  --max-mb 100 ^
  --cdn-base /imgs ^
  --title "研发部图床" ^
  --open
```

| 参数 | 说明 | 默认 |
|---|---|---|
| `--port` / `-p` | 监听端口 | `8899` |
| `--bind` / `-b` | 监听地址 | `0.0.0.0`（局域网可访问） |
| `--dir` / `-d` | 图片存放目录 | `./images` |
| `--max-mb` | 单文件大小上限（MB） | `50` |
| `--cdn-base` | 直链前缀。配相对路径（如 `/imgs`）可让图片与页面同源 | 空 |
| `--title` | 页面标题 | `内部图床` |
| `--readonly` | 只读：禁止上传和删除 | 关 |
| `--open` | 启动后自动开浏览器 | 关 |

## 接口（需要在别的程序里上传时用）

```bash
# 上传
curl -X PUT --data-binary @photo.png "http://192.168.67.24:8899/api/upload?name=photo.png"

# 列表
curl http://192.168.67.24:8899/api/list

# 删除
curl -X DELETE "http://192.168.67.24:8899/api/delete?name=photo.png"
```

在网页里上传后，返回的 `url` 字段就是完整直链。

### 直链的响应头（为什么能直接嵌到网页里）

- `Content-Disposition: inline` —— 浏览器显示图片，而不是弹下载框
- `Access-Control-Allow-Origin: *` —— 跨域 `<img>` 可加载
- `Accept-Ranges: bytes` + 支持 `Range` 请求 —— 视频可拖动进度条
- `Cache-Control: public, max-age=86400` —— 第二次访问走缓存

## 数据在哪

图片就是 `images` 目录下的普通文件，**直接拷走这个目录就等于备份**。
删掉 `imghost.py` 也不影响已存的图片——它们用任何 Web 服务器（IIS / Nginx / Caddy）都能继续提供访问。

## 建议

- **把 `images` 目录放到有备份的位置**（网络盘、定期复制），或改 `--dir` 指过去。
- 想开机自动启动、放行防火墙、一键装好：用下面的**一键部署**。
- 想让别人用域名访问（而不是 IP:端口）：用 IIS / Nginx / Caddy 反代到
  `http://127.0.0.1:8899`，启动时加 `--cdn-base http://你的域名`，
  这样「复制链接」给出的就是域名地址。
- 如果之后需要「谁传的、每人配额、账号密码」，再换 Zipline 那类完整图床；
  这个脚本刻意只做「丢图 + 拿链接」一件事。

## 一键部署（推荐）

双击 **`部署图床.cmd`**，会自己弹出管理员授权。它做四件事：

1. 把程序装到 `D:\imghost`（可用 `-DeployDir` 改）
2. 注册开机自启（计划任务，不开登录也能起；不行就自动退回「登录后启动」，再不行退回启动文件夹）
3. 放行防火墙端口 8899，同事才能访问
4. 启动并做一次健康检查，失败会打印排查命令

跑完会打印可以直接发给同事的局域网地址。

**常用写法**（在 `imghost` 目录下开 PowerShell）：

```powershell
# 默认安装：D:\imghost，端口 8899，开机自启
powershell -ExecutionPolicy Bypass -File deploy.ps1

# 换目录、换端口、图片存到网络盘
powershell -ExecutionPolicy Bypass -File deploy.ps1 -DeployDir D:\imghost -Port 8900 -ImageDir "Z:\图库"

# 不装开机自启 / 不动防火墙
powershell -ExecutionPolicy Bypass -File deploy.ps1 -NoAutostart -NoFirewall

# 让它在「用户登录后」启动，而不是开机就起
powershell -ExecutionPolicy Bypass -File deploy.ps1 -AtLogon

# 卸载（图片目录会保留，不会被删）
powershell -ExecutionPolicy Bypass -File deploy.ps1 -Uninstall
```

| 参数 | 说明 | 默认 |
|---|---|---|
| `-DeployDir` | 程序安装位置 | `D:\imghost` |
| `-Port` | 监听端口 | `8899` |
| `-ImageDir` | 图片目录 | `<DeployDir>\images` |
| `-TaskName` | 计划任务名 | `imghost` |
| `-AtLogon` | 改成登录后启动 | 关（默认开机启动） |
| `-NoAutostart` | 不配置自启，只启动一次 | 关 |
| `-NoFirewall` | 不动防火墙 | 关 |
| `-Uninstall` | 卸载自启和防火墙规则 | — |

重复运行是安全的：会先停旧实例，再覆盖程序文件，**图片一张都不会动**。

> 部署脚本只用 Windows 自带能力（计划任务 + 防火墙 + 复制文件），
> 不需要装任何东西。没有管理员权限时会自动降级——不装开机自启、不碰防火墙，
> 但图床照常跑起来。

## 环境要求

- Windows 7 及以上 / Linux / macOS，**Python 3.8+**（只用标准库，无需 pip install）。
- 本机已验证环境：Windows 10 专业版 + Python 3.14.2。
- 不需要 Docker、不需要数据库、不需要联网、不需要 openssl。

## 和别的方案比

| 方案 | 免登录 | 拖图即传 | 复制链接按钮 | 单文件零依赖 |
|---|---|---|---|---|
| **imghost** | ✅ | ✅ | ✅ | ✅ 一个 .py |
| dufs | ✅ | ✅ | ❌ 只能右键 | ✅ |
| miniserve | ✅ | ✅ | ❌ | ✅ |
| CloudFlare ImgBed | ✅ | ✅ | ✅ | ❌ 要 Docker |
| Zipline | ❌ 要账号 | ✅ | ✅ | ❌ 要 Docker+PG |

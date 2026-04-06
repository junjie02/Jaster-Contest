## 区域指引：
- web探测前期应重点收集：页面源代码 ; HTTP请求 ; robots协议 ; 文件/源码泄露 ; git泄露 ; 路径扫描 ; 域名解析。

## Knowledge Base: Web渗透测试核心知识点

### 1️⃣ 信息收集
- 基础侦察：页面源代码分析 | HTTP请求/响应包解读 | robots.txt协议
- 敏感泄露：文件/源码泄露 | .git/.svn泄露 | 备份文件探测
- 主动探测：路径/目录扫描 | 子域名枚举 | 域名解析记录(AXFR/DNSdumpster)

### 2️⃣ HTTP协议进阶
- 请求头伪造：X-Forwarded-For | Referer | User-Agent | Host注入
- 响应分析：状态码含义 | 重定向链 | 响应头安全策略(CSP/HSTS)

### 3️⃣ 暴力破解
- 字典策略：弱口令组合 | 行业默认密码 | 社工库关联
- 高级技巧：哈希生日攻击 | 验证码识别/绕过 | 并发速率控制

### 4️⃣ SQL注入
- 注入类型：数字型 | 字符型 | 搜索型 | 插入/更新注入
- 利用方式：联合查询 | 盲注(布尔/时间) | 报错注入 | 堆叠注入
- 高级利用：宽字节注入 | 二次注入 | 约束攻击 | UDF提权 | WAF Bypass

### 5️⃣ XSS跨站脚本
- 类型分类：反射型 | 存储型 | DOM型 | 混合型
- 进阶利用：XSS Bot | 钓鱼劫持 | Cookie窃取 | WAF Bypasspayload构造

### 6️⃣ 文件上传漏洞
- 绕过技术：前端验证绕过 | MIME/扩展名黑名单绕过 | 截断(%00/.%20)
- 高级Bypass：文件内容检查(魔术字节) | 条件竞争 | Windows特性(::$DATA/空格/点)
- 后续利用：Webshell管理 | 蚁剑/哥斯拉连接 | 权限维持

### 7️⃣ SSRF服务端请求伪造
- 协议利用：file:// | dict:// | gopher:// | http:// | ftp://
- 攻击目标：内网主机探测 | 云环境元数据读取 | 后台系统访问 | 组合利用(转Redis/MySQL)

### 8️⃣ 其他高危漏洞
- CSRF：Token绕过 | Referer伪造 | GET/POST转换 | 组合利用
- XXE：实体注入 | 文件读取 | 命令执行 | 无回显带外传输
- 逻辑漏洞：未授权访问 | 水平/垂直越权 | 并发竞争 | 业务流程绕过

### 9️⃣ 语言特性漏洞
#### PHP
- 特性利用：弱类型比较(==/===) | 变量覆盖($$) | 伪协议(file://|php://|data://)
- 代码执行：eval/assert/create_function | 命令执行(系统函数/反引号)
- 反序列化：__wakeup/__destruct魔法方法 | POP链构造 | phar反序列化
- 框架/模板：ThinkPHP/Laravel漏洞 | Twig/Smarty模板注入

#### Python
- 命令执行：os.system/subprocess/eval/exec | 沙箱逃逸(Jail Break)
- 模板注入：SSTI(Jinja2/Tornado) | 对象遍历(__class__/__mro__)
- 其他：反序列化(pickle) | Flask PIN码计算 | 调试模式泄露

#### Java
- 命令执行：Runtime.exec | ProcessBuilder | EL表达式
- 反序列化：Commons-Collections | Fastjson | Jackson RCE
- 框架漏洞：Spring SpEL | Shiro/CVE漏洞 | 代码审计要点

#### Node.js
- 语言特性：原型链污染 | 事件循环竞争 | 路径遍历
- 沙箱逃逸：VM模块绕过 | eval/new Function利用
- 反序列化：node-serialize | 模板注入

#### Golang
- 模板注入：html/template未转义 | 自定义函数利用
- 组件特性：Gin/Beego框架漏洞 | goroutine竞争
- 命令执行：os/exec | 反序列化风险

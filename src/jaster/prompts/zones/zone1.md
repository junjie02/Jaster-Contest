## 区域指引：
- web探测前期应重点收集：页面源代码 ; HTTP请求 ; robots协议 ; 文件/源码泄露 ; git泄露 ; 路径扫描 ; 域名解析。

## web flag常见路径
1.flag名称,根据实际情况在比赛中自由发挥：
../../../../../../../../../flag(.txt|.php|.pyc|.py)
../../../../../../../../../tmp/flag(.txt|.php|.pyc|.py)
../../../../../../../../../home/flag(.txt|.php|.pyc|.py)
../../../../../../../../../etc/flag(.txt|.php|.pyc|.py)
../../../../../../../../../root/flag(.txt|.php|.pyc|.py)
2.服务信息（绝对路径）
（1）/etc 目录
/etc/passwd :linux系统保存用户信息及其工作目录的文件，权限是可读。
/etc/shadow:是linux系统保存用户信息及（可能存在）密码（hash）的文件，权限是root用户可读写。
/etc/apache2/*  :Apache配置文件，可以获知WEB目录，服务端口等信息。
/etc/nginx/*  :Nginx配置文件，可以获知WEB目录，服务端口等信息。
/etc/apparmor.(d)/*  Apparmor配置文件，可以获得各应用系统调用的白名单、黑名单。
/etc/(cron.d/*|crontab)  :定时任务文件
/etc/environment 环境变量的配置文件之一。
/etc/hostname 表示主机名
/etc/hosts  是主机名查询静态列表，包含指定域名解析IP的成对信息。
/etc/issue 指明系统版本
/etc/mysql/*  MYSQL配置文件。
/etc/php/* PHP配置文件
（2）/proc目录
/proc 目录通常存储着进程动态运行的各种信息，本质上是一种虚拟目录。如果查看非当前进程的信息，pid是可以进行暴力破解的，如果查看的是当前进程，只需要/proc/self代替/proc/[pid]即可。
对应目录下的cmdline可读出比较敏感的信息，如使用mysql-uxxx -pxxx登陆mysql时，可以读出明文密码。
/proc/[pid]/cmdline   ([pid]指向进程对应的终端命令)
当我们无法获取当前应用所在的目录，通过cwd命令可以直接跳转到当前目录：
/proc/[pid]/cwd/     ([pid] 指向进行运行目录)
环境变量中可能存在secret_key,这时可以通过environ进行读取：
/proc/[pid]/environ  ([pid]指向进行运行时的环境变量）
（3）其他目录
Nginx配置文件可能存在的其他路径:
/usr/local/nginx/conf/*
日志文件：
/var/log/*
Apache 默认web根目录：
/var/www/html/
PHP session 目录：
/var/lib/php(5)/sessions/
用户目录：
[user_dir_you_know]/.bash_history (泄露历史执行命令)
[user_dir_you_know]/.bashrc  (部分环境变量)
[user_dir_you_know]/.ssh/id_rsa(.pub) (ssh 登陆私钥公钥)
[user_dir_you_know]/.viminfo (vim使用记录)
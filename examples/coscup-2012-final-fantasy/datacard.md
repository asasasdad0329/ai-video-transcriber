---
doc_id: datacard-1bacb9245985-0789e4bb
title: '[COSCUP 2012] Lightning Talk - 在 Final Fantasy 還沒變成 太空戰士 以前 - V字龍'
doc_type: video_datacard
language: zh-Hant
source:
  video: '[COSCUP 2012] Lightning Talk - 在 Final Fantasy 還沒變成 太空戰士 以前 - V字龍.mp4'
  duration: 05:06
  transcript_run: 1bacb9245985-0789e4bb
keywords:
- Final Fantasy
- 太空戰士
- COSCUP
- APT
- RPM
- Software Package Management System
- 固態硬碟
- 說明書
- 倉庫
- Software Repositories
- Debian
- script
- package
- 包裹
- 套件
sections: 7
unresolved:
- 節: S01
  原文: 然後這一個文件之後會開放編輯在Google Talk上面〔？〕。
- 節: S04
  原文: 然後Packagers就是把軟體建構好，然後把它包成Software Package，然後就會有交給有可信任的人，把它上傳到Software Repositories，所以我們可以把這個桌子〔？〕……
- 節: S07
  原文: 那我這邊推薦一個《BBS鄉民的正義》，聽到看〔？〕。
differences:
- 節: S03
  口述: 一天後，Oh God！
  畫面: OH FU__!
---

# [COSCUP 2012] Lightning Talk - 在 Final Fantasy 還沒變成 太空戰士 以前 - V字龍

## 目錄

| 節 | 時間 | 標題 | 摘要 |
| --- | --- | --- | --- |
| S01 | 00:00–00:54 | 開場 | 我今天要分享的是在太空戰士……欸，Final Fantasy 還沒變成太空戰士。 |
| S02 | 00:54–01:18 | 先來介紹一些東西：APT 與 RPM | Software Package Management System，那它是用來管理系統裡面安裝的軟體。 |
| S03 | 01:18–02:20 | 以下情節由真人真事改編 | 就是你拿到的一定不會是固態硬碟的本體，它會給你一個包裹 說明書怎麼裝就把它怎麼拆。 |
| S04 | 02:20–03:27 | 最後再回到這些東西：完全一致 | 最後再回到這些東西，其實我已經講完了，完全一致。 |
| S05 | 03:28–03:40 | 現在的情形 | But we refuse to call it package. We call it 套件。 |
| S06 | 03:40–04:15 | 「套件」有什麼問題嗎？ | 套件這個詞有辦法描述出剛剛我講的 可以描述這些東西嗎？ |
| S07 | 04:18–05:00 | 意見調查與感謝 | 以上是真的故事。 |

## S01｜開場｜00:00–00:54

強哥我們對你很好喔，下一位太空戰士，啊不是，V字龍。

好，大家好。

> 畫面：標題頁
>
> 在 Final Fantasy 還沒變成 太空戰士 之前……  
> Fork me on Google Drive!  
> `http://goo.gl/UyKvY`  
> V字龍 | Vdragon  
> `（email 略）`  
> COSCUP 2012 Lightning Talk
>
> 畫面註：右下角一直被講者子母畫面遮住，無法確認該處是否另有文字

我今天要分享的是在太空戰士……欸，Final Fantasy 還沒變成太空戰士。然後我是第一次來COSCUP。

> 畫面：授權條款
>
> 此作品所使用的授權條款  
> License applied to this work  
> 文字內容  
> Creative Commons CC-BY授權條款 3.0台灣版本  
> `http://creativecommons.org/licenses/by/3.0/tw/`  
> 圖像內容  
> 若無額外說明，本作品依據「合理使用」原則使用其他著作者的圖像內容，惟因多種因素限制無法先行請求授權，敬請見諒。如果有任何相關需求敬請不吝連繫本人。  
> 歡迎於授權條款的要求下使用本作品的內容，本作品位於下列網路位址：  
> `http://goo.gl/UyKvY`
>
> 三個項目依序為：文字內容（授權條款與網址）、圖像內容（合理使用說明）、作品網址。

然後也是第一次來閃電秀，所以我八成會失敗，那講到哪裡就講到哪裡。然後這一個文件之後會開放編輯在Google Talk上面〔？〕。

> 畫面：達成成就
>
> 達成成就  
> Unlocked achievements  
> - 連學校作業都想放到GitHub上的大學生(咦  
> - 拿到開放原始碼貢獻者VIP票但是只有發過一個跟程式碼有關的commit  
> - 活動範圍  
>   - Vdragon  
>   - pika1021
>
> 活動範圍旁列出網站標誌：GitHub（SOCIAL CODING）、Plurk、Facebook、ubuntu 正體中文站、Launchpad、Google。右側是一張數碼寶貝卡片圖（エアロブイドラモン，BANDAI）。
>
> 畫面註：Ubuntu 標誌下方的小字讀作「正體中文站」，解析度低，不完全確定；卡片上的小字不清

然後我是自我介紹我就跳過了，我怕沒時間。

## S02｜先來介紹一些東西：APT 與 RPM｜00:54–01:18

```yaml
術語:
- 詞: Software Package Management System
  原文: Software Package Management System，那它是用來管理系統裡面安裝的軟體。
```

> 畫面：先來介紹一些東西：APT 與 RPM
>
> 先來介紹一些東西……  
> First, introduce some things...  
> APT  
> Advanced Packaging Tool  
> `$sudo「你喜歡的文字編輯器」/etc/apt/sources.list`  
> `$sudo apt-get update`  
> `$sudo apt-get upgrade`  
> `$sudo apt-get install Kingston-SSD`  
> `$sudo apt-get remove Kingston-SSD`  
> RPM  
> RPM Package Manager  
> `$sudo「你喜歡的文字編輯器」/etc/yum.conf`  
> `$sudo yum check-update`  
> `$sudo yum update`  
> `$sudo yum install Kingston-SSD`  
> `$sudo yum remove Kingston-SSD`
>
> 畫面分兩步出現：先只有標題與 APT、RPM 名稱，之後兩組指令出現在各自右側。
>
> 畫面註：指令字很小，「你喜歡的文字編輯器」為最佳判讀，RPM 那行尤其模糊；$ 與 sudo 之間是否有空格無法確認

好，我們先來介紹一些東西，那APT、RPM大家應該多少都會用過，如果還沒看過的話，那這些指令大家都應該用過了。然後這個東西是……

> 畫面：Software Package Management System
>
> 先來介紹一些東西……  
> First, introduce some things...  
> Software Package Management System

Software Package Management System，那它是用來管理系統裡面安裝的軟體。那我們先別講這些東西。

> 畫面：先別提這些東西
>
> 然後……先別提這些東西(喂！  
> Then...yet don't mention these things(Hey!  
> APT  
> Advanced Packaging Tool  
> RPM  
> RPM Package Manager
>
> 版面與前面的 APT／RPM 頁相同，兩組指令也都在；APT、Advanced Packaging Tool、RPM、RPM Package Manager 與大部分指令加上刪除線。標題的左括號在畫面上沒有閉合。
>
> 畫面註：哪幾行指令有刪除線，解析度不足以可靠判讀

然後我們來講一個真人真事改編的故事。

## S03｜以下情節由真人真事改編｜01:18–02:20

```yaml
案例:
- 名稱: 固態硬碟
  品牌: Kingston
  型號: SSDNow V+ 200
  容量: 120GB
  原文:
  - 那我們就要把它解除安裝，拿去RMA。
  - 說明書怎麼裝就把它怎麼拆。
```

> 畫面：以下情節由真人真事改編
>
> 以下情節由真人真事改編  
> 固態硬碟用完了，所以我跑去添購……  
> I ran out of SSD, so I went to buy one...

我固態硬碟用完了，所以我跑去添購，那我知道這個固態硬碟一定是有人先設計。

> 畫面：Designer & builders
>
> Designer & builders  
> - Designer  
> - Manufacturers
>
> 清單下方有扳手與螺絲起子圖示。

然後由廠商把它製造出來，那它一定會包裝。

> 畫面：Packagers & repository uploaders
>
> Packagers & repository uploaders
>
> 只有標題與一個紙箱圖示。

就是你拿到的一定不會是固態硬碟的本體，它會給你一個包裹，然後一定會有可信任的人把它放到那個店家的倉庫，你不能說隨便讓什麼人就把東西放進去這樣子。然後倉庫大概長這樣子。

> 畫面：倉庫 Repositories
>
> 倉庫  
> Repositories  
> 圖片來源 `http://moodleman.moodle.com.au/archives/〔不清〕`，此網頁介紹 Moodle 的 Repository API  
> I heard someone thinks...Git?  
> Yes its the same way ;)
>
> 標題下是倉庫貨架與堆高機的照片，右下角是關於 Git 的兩行字。
>
> 畫面註：圖片來源網址很小，/archives/ 之後的路徑不清，主機名稱為最佳判讀

那Git其實也是這個東西。那你當然要去店家買。

> 畫面：獲取
>
> 獲取……  
> Retrieving
>
> 只有標題與一張城市街景照片（汽車、黃色計程車、公寓、工地吊車），照片裡沒有可讀文字。

但是就是把商品拿回來，然後你要一個剪刀什麼的把商品解開來。

> 畫面：解開：固態硬碟包裝
>
> 解開……  
> Extracting...  
> Kingston  
> SSDNow V+ 200  
> 120GB
>
> 金士頓 SSDNow V+200 120GB 固態硬碟的吊卡包裝照片：上方卡片有品牌、型號與讀寫速度欄位，硬碟標籤印著 Kingston、120GB、SSDNow V+ 200。
>
> 畫面註：讀寫速度數值與包裝上的小字不清；V+ 後面的 200 為最佳判讀

那一定會有什麼說明書。

> 畫面：安裝：說明書
>
> 安裝……  
> Installing...  
> Kingston TECHNOLOGY  
> SSDNOW SERIES  
> Solid State Drive  
> Getting Started
>
> 展開的金士頓 SSDNow 安裝說明書照片，畫有把硬碟裝進桌機機殼與支架的線圖。
>
> 畫面註：說明書上的版權與頁尾小字不清

叫說明書要怎麼把那個固態硬碟裝到電腦裡面去，然後就可以直接安裝，然後就開始使用了。

> 畫面：一天後
>
> 一天後……  
> One day later...

一天後，Oh God！

> 畫面：喔__！
>
> 喔__！  
> OH FU__!
>
> 黑底白字；底線是畫面上原有的消音記號。

它壞掉了。

> 畫面：解除安裝：說明書
>
> 解除安裝……  
> De-installing...  
> Kingston TECHNOLOGY  
> SSDNOW SERIES  
> Solid State Drive  
> Getting Started
>
> 與「安裝」頁相同的說明書照片，只換了標題。

那我們就要把它解除安裝，拿去RMA。那也是一樣，說明書怎麼裝就把它怎麼拆。

## S04｜最後再回到這些東西：完全一致｜02:20–03:27

```yaml
術語:
- 詞: Developer
  原文: 那Developer就是開發軟體的人，然後就是設計軟體的人。
- 詞: Packagers
  原文: 然後Packagers就是把軟體建構好，然後把它包成Software Package
- 詞: sources.list
  原文: 那其實這個網址就是我們從哪裡獲得這些Software Package。
- 詞: 獲取
  原文: 獲取也就是下載，就是從網址上面把它載下來這樣子。
- 詞: script
  原文: 裡面就會有script，這個就是跟說明書一樣，就是要把軟體給它安裝上去
案例:
- 名稱: sl
  套件: sl
  版本: 3.03-16
  架構: i386
  原文:
  - 然後解除安裝，一樣也是有script可以做這個事情，就跟說明書一樣。
```

> 畫面：最後再回來這些東西：完全一致
>
> 最後再回來這些東西……  
> Last, we come back to these things...  
> APT  
> Advanced Packaging Tool  
> `$sudo「你喜歡的文字編輯器」/etc/apt/sources.list`  
> `$sudo apt-get update`  
> `$sudo apt-get upgrade`  
> `$sudo apt-get install Kingston-SSD`  
> `$sudo apt-get remove Kingston-SSD`  
> RPM  
> RPM Package Manager  
> `$sudo「你喜歡的文字編輯器」/etc/yum.conf`  
> `$sudo yum check-update`  
> `$sudo yum update`  
> `$sudo yum install Kingston-SSD`  
> `$sudo yum remove Kingston-SSD`  
> 完全一致！
>
> 畫面分兩個狀態：先顯示與第一頁相同的 APT、RPM 兩組指令；之後 RPM 區塊換成大字「完全一致！」，APT 區塊與指令保留。
>
> 畫面註：指令字很小，「你喜歡的文字編輯器」為最佳判讀

最後再回到這些東西，其實我已經講完了，完全一致。

> 畫面：Developers
>
> Developers
>
> 只有標題與跟 Designer & builders 頁相同的扳手與螺絲起子圖示。

那Developer就是開發軟體的人，然後就是設計軟體的人。

> 畫面：Packagers & repository uploaders：Debian 開發者
>
> Packagers & repository uploaders  
> 我相信這邊有非常多的Debian開發者……  
> I believe there are many Debian Developers here...
>
> 文字下方是紙箱圖示。

然後Packagers就是把軟體建構好，然後把它包成Software Package，然後就會有交給有可信任的人，把它上傳到Software Repositories，所以我們可以把這個桌子〔？〕……

> 畫面：倉庫 Repositories：sources.list
>
> 倉庫  
> Repositories  
> 圖片來源 `http://moodleman.moodle.com.au/archives/〔不清〕`，此網頁介紹 Moodle 的 Repository API  
> `$sudo vim /etc/apt/sources.list`  
> ```  
> ## N.B. software from this repository may not have been tested as  
> ## extensively as that contained in the main release, although it includes  
> ## newer versions of some applications which may provide useful features.  
> ## Also, please note that software in backports WILL NOT receive any review  
> ## or updates from the Ubuntu security team.  
>   
> deb http://free.nchc.org.tw/ubuntu/ precise-security main restricted  
> deb-src http://free.nchc.org.tw/ubuntu/ precise-security main restricted  
> deb http://free.nchc.org.tw/ubuntu/ precise-security universe  
> deb-src http://free.nchc.org.tw/ubuntu/ precise-security universe  
> deb http://free.nchc.org.tw/ubuntu/ precise-security multiverse  
> deb-src http://free.nchc.org.tw/ubuntu/ precise-security multiverse  
> ```
>
> 由上而下：圖片來源、編輯指令、黑底終端機裡的 sources.list 片段（綠色註解、紫色網址、黃色版本與元件），最下方是縮小的倉庫照片。
>
> 畫面註：圖片來源網址 /archives/ 之後的路徑不清

就是倉庫齁。大家都應該多少有改過，`sources.list`，那其實這個網址就是我們從哪裡獲得這些Software Package。

> 畫面：獲取（也就是下載）
>
> 獲取(也就是下載)……  
> Retrieving(Yes, its download here)...  
> ```  
> 下載:1 http://free.nchc.org.tw/ubuntu/ precise/universe sl i386 3.03-16 [25.3 kB]  
> 取得 25.3 kB 用了 0秒 (209 kB/s)  
> ```
>
> 白底中央一條細長的黑色終端機畫面，綠字顯示兩行 apt 下載輸出，第三行被下緣切掉。
>
> 畫面註：終端機字極小，網址裡的斜線與 25.3 kB 外的方括號依 apt 的標準輸出格式判讀；第二行開頭的「取得」略模糊；第三行被切掉，內容不清

獲取也就是下載，就是從網址上面把它載下來這樣子。解開，就是……

> 畫面：解開：終端機
>
> 解開……  
> Extracting...  
> `正在解開 sl (從 .../archives/sl_3.03-16_i386.deb) ...`
>
> 畫面註：終端機小字，括號是全形還是半形無法確定

就跟那個一樣這樣子。

> 畫面：安裝：終端機
>
> 安裝……  
> Installing...  
> ```  
> 正在進行 man-db 的觸發程式 ...  
> 正在設定 sl (3.03-16) ...  
> localepurge: Disk space freed in /usr/share/locale: 0 KiB  
> localepurge: Disk space freed in /usr/share/man: 36 KiB  
> localepurge: Disk space freed in /usr/share/gnome/help: 0 KiB  
> localepurge: Disk space freed in /usr/share/omf: 0 KiB  
> localepurge: Disk space freed in /usr/share/doc/kde/HTML: 0 KiB  
>   
> Total disk space freed by localepurge: 36 KiB  
>   
> Vdragon@Vdragon-SSD01-Ubuntu:~$  
> ```  
> postinst　prerm
>
> 終端機下方有兩個檔案圖示，分別標示 postinst 與 prerm。

然後安裝。那如果我們把Debian Software Package把它解開來的話，裡面就會有script，這個就是跟說明書一樣，就是要把軟體給它安裝上去，就是安裝。

> 畫面：一天後（第二次）
>
> 一天後……  
> One day later...

一天後，OK。

> 畫面：喔__！（第二次）
>
> 喔__！  
> OH FU__!
>
> 黑底白字；底線是畫面上原有的消音記號。

它壞掉了。

> 畫面：解除安裝：apt-get remove
>
> 解除安裝……  
> De-installing...  
> ```  
> Vdragon@Vdragon-SSD01-Ubuntu:~$ sudo apt-get remove sl  
> 正在讀取套件清單... 完成  
> 正在重建相依關係  
> 正在讀取狀態資料... 完成  
> 下列套件將會被【移除】:  
>   sl  
> 升級 0 個，新安裝 0 個，移除 1 個，有 1 個未被升級。  
> 此操作完成之後，會空出 193 kB 的磁碟空間。  
> 是否繼續進行 [Y/n]？Y  
> (正在讀取資料庫 ... 278576 files and directories currently installed.)  
> 正在移除 sl ...  
> 正在進行 man-db 的觸發程式 ...  
> Vdragon@Vdragon-SSD01-Ubuntu:~$  
> ```  
> postinst　prerm
>
> 終端機下方同樣有 postinst 與 prerm 兩個檔案圖示。
>
> 畫面註：193 kB 與 278576 兩個數字來自低解析度小字

然後解除安裝，一樣也是有script可以做這個事情，就跟說明書一樣。現在的情形。

## S05｜現在的情形｜03:28–03:40

> 畫面：現在的情形
>
> 現在的情形  
> Facts about NOW
>
> 畫面上方中央有一個小紙箱圖示。

It looks like a package.

> 畫面：它看起來很「包裹」
>
> 它看起來很「包裹」  
> It looks like "package"  
> 圖示來源:  
> `/usr/share/icons/gnome/256x256/mimetypes/package-x-generic.png`  
> @ Ubuntu 12.04 LTS
>
> 中央是紙箱圖示；「看起來」與 looks 以青色標示。
>
> 畫面註：圖示路徑字很小，package-x-generic 的分隔符號判讀為連字號

We are using it like package.

> 畫面：我們用的很「包裹」
>
> 我們用的很「包裹」  
> We're using it like "package"
>
> 放大的紙箱圖示；「用的」與 using 以青色標示。

But we refuse to call it package.

> 畫面：但是我們拒絕稱呼它為「包裹」
>
> 但是我們拒絕稱呼它為「包裹」  
> But we refused to call it "package"
>
> 紙箱圖右側有手繪火柴人拿鉛筆在箱面塗寫；「拒絕」與 refused to 為紅字。
>
> 畫面註：箱面上的手寫字跡不清

We call it 套件。

> 畫面：我們稱呼它為「套件」
>
> 我們稱呼它為「套件」  
> We called it "套件"  
> 套件
>
> 火柴人拿著鉛筆，箱面寫著「套件」。

套件有什麼問題嗎？

## S06｜「套件」有什麼問題嗎？｜03:40–04:15

```yaml
術語:
- 詞: 套
  原文: 套可能有set跟suite的意思
- 詞: 件
  原文: 那件可能有piece或ware的意思
```

> 畫面：「套件」有什麼問題嗎？
>
> 「套件」有什麼問題嗎？  
> What is wrong with "套件"?  
> 套件
>
> 同前一頁的紙箱圖，箱面寫「套件」，火柴人旁多了一個問號。

如果從字面上來看的話：

> 畫面：「套件」有什麼問題嗎？set、suite、piece、ware
>
> 「套件」有什麼問題嗎？  
> What is wrong with "套件"?  
> 套件  
> set?　A set of files...hmm  
> suite?　LibreOffice, Apache OpenOffice, Microsoft Office, ......  
> piece?　A piece of software...hmm  
> ware?　A software...hmm  
> 看起來好像沒什麼問題……  
> Seems like nothing wrong...
>
> 左側是直排大字「套件」，右側四列依序對應 set、suite、piece、ware 及各自的說明；最下方是「看起來好像沒什麼問題……」。

套可能有set跟suite的意思，那件可能有piece或ware的意思，看起來好像沒什麼問題，好像都可以拿來描述軟體。那……

> 畫面：「套件」感覺比較專業
>
> 「套件」感覺比較專業……  
> "套件" seems much professional...  
> 國家教育研究院 學術名詞資訊網  
> 8　電子計算機　Berkaley EDIF200  
> 9　電子計算機　apt-get  
> 10　電子計算機　applications software kit , AS〔不清〕  
> 共 85 筆資料 | 每頁 10 筆 | 共 9 頁　跳至 [　] 頁 GO  
> `http://goo.gl/Vnze9`  
> 柏克萊翻譯器套件；柏克萊 E〔不清〕套件  
> apt-get 套件  
> 應用軟體套件  
> 好專業！
>
> 左側是學術名詞資訊網搜尋結果的截圖，畫面只看得到第 8 至 10 筆、看不到欄位標題；右側三個中文譯名裡的「套件」為紅字，「apt-get 套件」加了紅圈，旁邊是手寫風格的「好專業！」。
>
> 畫面註：Berkaley 照畫面保留，可能是來源本身的錯字；第三筆與右側第一行被截圖邊緣切掉；柏克萊翻譯器套件後的符號可能是；或：

套件感覺比較專業，apt-get套件，國家認證的智慧，好專業。套件的問題。

> 畫面：「套件」的問題
>
> 「套件」的問題  
> What is wrong with "套件"  
> 套件
>
> 畫面分兩步：先是紙箱消失，只剩火柴人拿鉛筆與「套件」兩個字；之後出現虛線的紙箱輪廓與一個問號。

套件這個詞有辦法描述出剛剛我講的，就是從獲取包裹，然後作成包裹，然後傳送給包裹，然後抓下來安裝，可以描述這些東西嗎？

真的可以嗎？

## S07｜意見調查與感謝｜04:18–05:00

> 畫面：意見調查？
>
> 意見調查？  
> - There will be one.  
> BBS 鄉民的正義  
> 2012 . 8 . 17
>
> 右側是電影海報，片名「BBS 鄉民的正義」，底部日期 2012.8.17。
>
> 畫面註：海報片名是美術字，依圖判讀為「BBS 鄉民的正義」；海報上方的演職員與英文副標題等小字不清

有人問說那個為什麼你們一直不，不做個意見調查？我相信會有。那我這邊推薦一個《BBS鄉民的正義》，聽到看〔？〕。

> 畫面：感謝 Credits
>
> 感謝  
> Credits  
> - Fred  
>   有史以來最棒的Hacker！  
> - 依瑪貓(imacat)  
>   有史以來最專業的軟體翻譯工作者！  
> - 魏藥(medicalwei)  
>   有史以來最會吐程式碼的貓咪！  
> - 聚乳酸(xatierlike)  
>   有史以來最黑的噗浪好友！  
> - 真正加(realplus)  
>   有史以來最真正的加！  
> - 台灣駭客年會(HITCON)*  
>   有史以來最好用的贊助商介紹梗！  
> *This page is not intended to have any implication with either HITCON or any other individuals.
>
> 聚乳酸旁有一個小圓臉圖示。

好感謝，Fred，有史以來最棒的hacker。依瑪貓，有史以來最專業的軟體翻譯工作者。魏藥，有史以來最會吐程式碼的貓咪。聚乳酸，有史以來最黑的噗浪好友。真正加，有史以來最真正的加。還有台灣hacker年會，有史以來最好用的贊助商介紹梗。然後……

> 畫面：感謝 Credits：Kingston
>
> 感謝  
> Credits  
> Kingston TECHNOLOGY  
> - 金士頓(Kingston)  
>   有史以來最專業的固態硬碟製造商！  
> *This page is not intended to have any implication with either HITCON or any other individuals.
>
> 左側是一張發票影像，右側是 Kingston TECHNOLOGY 標誌。
>
> 畫面註：發票上的細字不清；金士頓下方那行字解析度低，「專業」「固態」「製造」為最佳判讀

金士頓，有史以來最專業的固態硬碟製造商。以上是真的故事。那我希望他趕快換一個能用的給我，這樣子。感謝聆聽。

> 畫面：感謝聆聽
>
> 感謝聆聽！  
> Thanks for listening!  
> 0w0/  
> 2012/08/18, 19 @ COSCUP 2012

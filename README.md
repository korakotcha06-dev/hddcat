# HDDCAT 🐈💾 — Every File You Own. One Search Away.

สแกนฮาร์ดดิสก์ทุกลูกของคุณเข้า catalog เดียว ค้นเจอทุกไฟล์ในเสี้ยววินาที
โดยไม่ต้องเสียบไดรฟ์ — ข้อมูลทั้งหมดอยู่ในเครื่องคุณ 100% ไม่มี cloud

## Screenshot

_(coming soon)_

## วิธีเปิดใช้ (macOS)

1. แตกไฟล์ zip นี้ไว้ที่ไหนก็ได้ (เช่น โฟลเดอร์ Applications หรือ Documents)
2. **คลิกขวา** ที่ไฟล์ `เปิด HDDCAT.command` แล้วเลือก **Open** (ครั้งแรกครั้งเดียว —
   macOS จะถามยืนยันเพราะไฟล์มาจากอินเทอร์เน็ต) ครั้งต่อไปดับเบิลคลิกได้เลย
3. เบราว์เซอร์จะเปิด HDDCAT ขึ้นมาเอง → ไปที่แท็บ "สแกน" เสียบไดรฟ์ แล้วเริ่มเก็บ catalog ได้ทันที

> ครั้งแรก ถ้าเครื่องยังไม่มี python3 ระบบจะเด้งหน้าต่างชวนติดตั้ง
> "Command Line Developer Tools" — กด Install รอสักครู่ แล้วเปิดใหม่อีกครั้ง

## ข้อมูลอยู่ที่ไหน?

ทุกอย่างอยู่ในไฟล์ `catalog.db` ข้างๆ ตัวโปรแกรมนี้ — ไม่มีอะไรถูกส่งออกจากเครื่องคุณ
อยากย้ายเครื่อง ก็ก๊อปทั้งโฟลเดอร์นี้ไปได้เลย

## ใช้จาก Terminal ก็ได้

    python3 catalog.py scan /Volumes/ไดรฟ์ของคุณ --label ชื่อไดรฟ์
    python3 catalog.py search คำค้น
    python3 catalog.py serve

---
MIT License · © 2026 Touchnewmedia Co., Ltd.
GitHub: https://github.com/korakotcha06-dev/hddcat · ☕ https://www.buymeacoffee.com/korakot

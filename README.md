# تنزيل وسائط تويتر/X

## التشغيل
```bash
pip install -r requirements.txt
python app.py            # ثم افتح http://localhost:8000
```
للإنتاج (عملية واحدة لأن الحالة في الذاكرة):
```bash
gunicorn -w 1 --threads 8 -b 0.0.0.0:8000 --timeout 120 app:app
```

## تسجيل الدخول (مطلوب)
تويتر لم يعد يعرض وسائط الحسابات للزوار. اختر واحدة:
1. صدّر كوكيز حسابك بصيغة Netscape إلى `cookies.txt` بجانب `app.py`
   (إضافة "Get cookies.txt LOCALLY" في المتصفح).
2. أو عيّن المتغير: `TWITTER_AUTH_TOKEN=قيمة_كوكي_auth_token`

يُفضّل استخدام حساب ثانوي، لا حسابك الأساسي.

## إعدادات اختيارية (متغيرات بيئة)
`MAX_ITEMS` (200) · `WORKERS` (2) · `JOB_TIMEOUT` (1800 ث) · `JOB_TTL` (3600 ث) · `PORT` (8000)

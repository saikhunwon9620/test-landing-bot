# Test Landing — Messenger AI Chatbot တပ်ဆင်နည်း

## အဆင့် ၁ — Facebook App ဖန်တီးပါ
1. https://developers.facebook.com ကို ဝင်ပါ
2. **My Apps → Create App** → အမျိုးအစား **Business** ရွေးပါ
3. App dashboard မှာ **Add Product → Messenger → Set up** နှိပ်ပါ

## အဆင့် ၂ — Page ချိတ်ပြီး Token ထုတ်ပါ
1. Messenger settings → **Access Tokens → Add or Remove Pages** → **Test Landing** page ကို ချိတ်ပါ
2. **Generate Token** နှိပ်ပြီး ရလာတဲ့ token ကို သိမ်းထားပါ — ဒါက `PAGE_ACCESS_TOKEN` ပါ
3. Token ကို ဘယ်သူနဲ့မှ မမျှပါနဲ့

## အဆင့် ၃ — Server တင်ပါ (Deploy)
ဒီကုဒ်ကို အင်တာနက်ပေါ်ကနေ ဝင်လို့ရတဲ့ server တစ်ခုမှာ တင်ရပါမယ်။
အလွယ်ဆုံးနည်းများ: **Railway** (railway.app), **Render** (render.com), သို့မဟုတ် **Fly.io**

1. ဒီ `test-landing-bot` folder ကို GitHub repo တစ်ခုအဖြစ် တင်ပါ
2. Railway/Render မှာ **New Project → Deploy from GitHub** ရွေးပါ (Dockerfile ကို အလိုအလျောက် သိပါတယ်)
3. Environment variables တွေ ထည့်ပါ:
   - `VERIFY_TOKEN` — ကိုယ်တိုင်ရွေးထားတဲ့ လျှို့ဝှက်စာသား (ဥပမာ: `tl-bot-2026-secret`)
   - `PAGE_ACCESS_TOKEN` — အဆင့် ၂ က token
   - `GEMINI_API_KEY` — https://aistudio.google.com/app/apikey က အခမဲ့ထုတ်ယူပါ
     (Google အကောင့်ပဲ လိုအပ်ပါတယ်၊ ကတ်မလိုပါ)
4. Deploy ပြီးရင် public URL ရပါမယ် — ဥပမာ `https://xxx.up.railway.app`

## အဆင့် ၄ — Webhook ချိတ်ပါ
1. Facebook App dashboard → Messenger → **Webhooks → Add Callback URL**
2. **Callback URL:** `https://သင့်-url/webhook`
3. **Verify Token:** အဆင့် ၃ မှာ ထည့်ထားတဲ့ `VERIFY_TOKEN` နဲ့ အတူတူဖြစ်ရမယ်
4. **Verify and Save** နှိပ်ပါ
5. **Subscription Fields** မှာ `messages` ကို check လုပ်ပါ (ဒါမှ စာဝင်လာတာ လက်ခံရမယ်)

## အဆင့် ၅ — စမ်းသပ်ပါ
1. Test Landing page ကို Messenger ကနေ စာပို့ကြည့်ပါ (ဥပမာ: "မင်္ဂလာပါ")
2. Bot က အဖြေပြန်ပို့ရင် အောင်မြင်ပါပြီ ✅
3. **သတိပြုရန်:** App က development mode မှာ ရှိနေရင် page role ရှိသူတွေဆီကပဲ စာလက်ခံပါမယ်။
   လူတိုင်းဆီက စာလက်ခံချင်ရင် App Review လျှောက်ပြီး `pages_messaging` permission ကို
   အတည်ပြုချက် ယူရပါမယ်။

## ဒေတာ train လုပ်နည်း
- `knowledge/` folder ထဲက `faq.md` ကို သင့်လုပ်ငန်းအချက်အလက်အမှန်တွေနဲ့ ဖြည့်ပါ
- ဖိုင်အသစ်တွေ ထပ်ထည့်လို့ရပါတယ် (`prices.md`, `services.md`...) — `.md` ဖိုင်တိုင်းကို bot က အလိုအလျောက် ဖတ်ပါတယ်
- ပြင်ဆင်ပြီးတိုင်း server ကို ပြန် deploy လုပ်ပါ (သို့မဟုတ် ပြန်စပါ)
- မေးခွန်းအစစ်တွေနဲ့ စမ်းပြီး မှားဖြေတာတွေ့ရင် knowledge ဖိုင်ထဲမှာ ပြင်ပါ — ဒါက train လုပ်တဲ့ အဓိကနည်းပါ

## အရေးကြီးသော လျှို့ဝှက်ချက်များ
`PAGE_ACCESS_TOKEN` နဲ့ `GEMINI_API_KEY` ကို ဘယ်တော့မှ လူမြင်ကွင်းမှာ မတင်ပါနဲ့၊
GitHub မှာ public repo ဆို `.env` ဖိုင် မထည့်မိပါစေနဲ့။

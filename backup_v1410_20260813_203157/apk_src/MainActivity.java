package com.smartlocker.screen;
import org.json.JSONArray;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ContentValues;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.TextUtils;
import android.util.Log;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.widget.*;

import com.smartlocker.screen.common.RunHalfView;
import com.smartlocker.screen.common.SignalView;
import com.smartlocker.screen.db.DBHelper;
import com.smartlocker.screen.service.LockerService;
import com.smartlocker.screen.utils.PreferencesHelper;

import com.google.zxing.BarcodeFormat;
import com.google.zxing.common.BitMatrix;
import com.google.zxing.qrcode.QRCodeWriter;

import org.json.JSONObject;

import okhttp3.*;

public class MainActivity extends Activity {
    private static final String TAG = "MainActivity";
    private static final int REFRESH_INTERVAL = 300000;

    private RunHalfView mRunHalfView;
    private SignalView mSignalView;
    private TextClock mTvCurrentTime;
    private TextView mTvStoreName;
    private TextView mTvBusinessHours;
    private TextView mTvAvailableBoxes;
    private TextView mTvDeviceId;
    private ImageView mIvQrCode;
    private TextView mTvPhone;
    private android.os.Handler mStatusChecker;
    private LinearLayout mBtnPickup;
    private LinearLayout mImgRefresh;
    private View mGuize1Include;
    private View mGuize5Include;

    private PreferencesHelper mPrefs;
    private DBHelper mDB;
    private OkHttpClient mOkClient;
    private Handler mRefreshHandler = new Handler(Looper.getMainLooper());
    private WebSocket mWebSocket;
    private android.content.BroadcastReceiver mRulesReceiver;

    private Runnable mRefreshRunnable = new Runnable() {
        @Override
        public void run() {
            refreshSlotStatus();
            mRefreshHandler.postDelayed(this, REFRESH_INTERVAL);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        mPrefs = new PreferencesHelper(this);

        if (!mPrefs.isRegistered() || TextUtils.isEmpty(mPrefs.getDeviceId())) {
            Intent setupIntent = new Intent(this, RegisterActivity.class);
            startActivity(setupIntent);
            finish();
            return;
        }

        hideSystemUI();
        mDB = DBHelper.getInstance(this);

        OkHttpClient.Builder okBuilder = new OkHttpClient.Builder()
                .connectTimeout(10, java.util.concurrent.TimeUnit.SECONDS);
        try {
            SSLSocketHelper.trustCustomCert(okBuilder);
        } catch (Exception e) {
            Log.w(TAG, "SSL cert trust setup: " + e.getMessage());
        }
        mOkClient = okBuilder.build();

        setContentView(R.layout.activity_main);
        // 动态显示版本号
        try {
            TextView tvVersion = findViewById(R.id.tvAppVersion);
            if (tvVersion != null) {
                String vName = getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
                tvVersion.setText("v" + vName);
            }
        } catch (Exception e) {
            Log.e(TAG, "setVersionText: " + e.getMessage());
        }
        initViews();
        initData();
        checkForUpdates();
        startServices();

        mRefreshHandler.postDelayed(mRefreshRunnable, REFRESH_INTERVAL);
    }

    private void hideSystemUI() {
        Window window = getWindow();
        window.setFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN,
                WindowManager.LayoutParams.FLAG_FULLSCREEN);
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        View decorView = window.getDecorView();
        decorView.setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_FULLSCREEN);
    }

    private void initViews() {
        mRunHalfView = findViewById(R.id.run_half_view);
        mSignalView = findViewById(R.id.signal_view);
        mTvCurrentTime = findViewById(R.id.textClock1);
        mTvStoreName = findViewById(R.id.tv_netname);
        mTvBusinessHours = findViewById(R.id.tv_time);
        mTvPhone = findViewById(R.id.tv_phone);
        mTvDeviceId = findViewById(R.id.tv_mid);
        mIvQrCode = findViewById(R.id.img_qrcode);
        mTvAvailableBoxes = findViewById(R.id.tv_door);
        mBtnPickup = findViewById(R.id.btn_finsh_btn);
        mImgRefresh = findViewById(R.id.img_refresh);

        // 密码取包
        if (mBtnPickup != null) {
            mBtnPickup.setOnClickListener(v -> showPickupDialog());
        }

        // 寄存规则视图
        mGuize1Include = findViewById(R.id.guize1);
        mGuize5Include = findViewById(R.id.guize5);

        // 刷新
        if (mImgRefresh != null) {
            mImgRefresh.setOnClickListener(v -> refreshSlotStatus());
        }

        // 长按左上角网点名称10秒进入管理界面
        if (mTvStoreName != null) {
            final String[] originalText = new String[1];
            mTvStoreName.setOnTouchListener(new View.OnTouchListener() {
                private Handler mAdminHandler = new Handler(Looper.getMainLooper());
                private Runnable mAdminRunnable;
                private int mAdminCountdown;
                @Override
                public boolean onTouch(View v, MotionEvent event) {
                    if (event.getAction() == MotionEvent.ACTION_DOWN) {
                        originalText[0] = mTvStoreName.getText().toString();
                        mAdminCountdown = 10;
                        mAdminRunnable = new Runnable() {
                            @Override
                            public void run() {
                                mAdminCountdown--;
                                if (mAdminCountdown > 0) {
                                    mTvStoreName.setText(originalText[0] + " (" + mAdminCountdown + "s)");
                                    mAdminHandler.postDelayed(this, 1000);
                                } else {
                                    mTvStoreName.setText(originalText[0]);
                                    openAdmin();
                                }
                            }
                        };
                        mAdminHandler.postDelayed(mAdminRunnable, 1000);
                        return true;
                    } else if (event.getAction() == MotionEvent.ACTION_UP || event.getAction() == MotionEvent.ACTION_CANCEL) {
                        mAdminHandler.removeCallbacks(mAdminRunnable);
                        if (originalText[0] != null) {
                            mTvStoreName.setText(originalText[0]);
                        }
                        return true;
                    }
                    return false;
                }
            });
        }
    }

    private void initData() {
        String deviceId = mPrefs.getDeviceId();
        String storeName = mPrefs.getStoreName();

        if (mTvStoreName != null) {
            mTvStoreName.setText(!TextUtils.isEmpty(storeName) ? storeName : "智能寄存柜");
        }
        if (mTvDeviceId != null) {
            mTvDeviceId.setText(!TextUtils.isEmpty(deviceId) ? deviceId : "--");
        }

        // 加载网点信息
        loadStoreInfo();

        // 生成二维码
        if (mIvQrCode != null && !TextUtils.isEmpty(deviceId)) {
            String qrUrl = "https://locker.cqdyxl.com/store?device=" + deviceId;
            try {
                QRCodeWriter qrWriter = new QRCodeWriter();
                int qrSize = 380;
                BitMatrix bitMatrix = qrWriter.encode(qrUrl, BarcodeFormat.QR_CODE, qrSize, qrSize);
                Bitmap qrBitmap = Bitmap.createBitmap(qrSize, qrSize, Bitmap.Config.RGB_565);
                for (int x = 0; x < qrSize; x++) {
                    for (int y = 0; y < qrSize; y++) {
                        qrBitmap.setPixel(x, y, bitMatrix.get(x, y) ? Color.BLACK : Color.WHITE);
                    }
                }
                mIvQrCode.setImageBitmap(qrBitmap);
            } catch (Exception e) {
                Log.e(TAG, "QR code generation failed: " + e.getMessage());
            }
        }

    }

    private void loadStoreInfo() {
        if (mDB == null) return;
        ContentValues storeInfo = mDB.getStoreInfo();
        if (storeInfo != null) {
            String name = storeInfo.getAsString("name");
            String hours = storeInfo.getAsString("hours");
            String phone = storeInfo.getAsString("phone");

            if (mTvStoreName != null && !TextUtils.isEmpty(name)) {
                mTvStoreName.setText(name);
            }
            if (mTvBusinessHours != null && !TextUtils.isEmpty(hours)) {
                mTvBusinessHours.setText(hours);
            }
            if (mTvPhone != null && !TextUtils.isEmpty(phone)) {
                mTvPhone.setText(phone);
            }
        }
    }

    private void updateAvailableDisplay() {
        if (mDB == null) return;
        ContentValues storeInfo = mDB.getStoreInfo();
        int available = 0;
        int total = mPrefs.getBoardCount() * 24;

        if (storeInfo != null) {
            Integer avail = storeInfo.getAsInteger("available");
            Integer tot = storeInfo.getAsInteger("total");
            if (avail != null) available = avail;
            if (tot != null && tot > 0) total = tot;
        }

        if (mTvAvailableBoxes != null) {
            mTvAvailableBoxes.setText(String.valueOf(available));
        }
        if (mRunHalfView != null) {
            mRunHalfView.setData(available, total);
        }
    }

    private void checkForUpdates() {
        String url = mPrefs.getServerUrl() + "/api/app/version";
        try {
            Request request = new Request.Builder()
                    .url(url)
                    .get()
                    .build();
            mOkClient.newCall(request).enqueue(new Callback() {
                @Override
                public void onFailure(Call call, java.io.IOException e) {
                    Log.e(TAG, "checkForUpdates failed: " + e.getMessage());
                }
                @Override
                public void onResponse(Call call, Response response) throws java.io.IOException {
                    try {
                        String respStr = response.body().string();
                        org.json.JSONObject json = new org.json.JSONObject(respStr);
                        if (json.optInt("code") == 200) {
                            org.json.JSONObject data = json.optJSONObject("data");
                            if (data != null) {
                                int newVersion = data.optInt("version_code", 0);
                                String versionName = data.optString("version_name", "");
                                String downloadUrl = data.optString("download_url", "");
                                String updateLog = data.optString("update_log", "");
                                int currentVersion = getPackageManager().getPackageInfo(getPackageName(), 0).versionCode;
                                if (newVersion > currentVersion && !TextUtils.isEmpty(downloadUrl)) {
                                    String fName = versionName;
                                    String fLog = updateLog;
                                    String fUrl = downloadUrl;
                                    runOnUiThread(() -> showUpdateDialog(fName, fLog, fUrl, false));
                                }
                            }
                        }
                    } catch (Exception e) {
                        Log.e(TAG, "checkForUpdates parse error: " + e.getMessage());
                    }
                }
            });
        } catch (Exception e) {
            Log.e(TAG, "checkForUpdates error: " + e.getMessage());
        }
    }

    private void showUpdateDialog(String versionName, String desc, String downloadUrl, boolean force) {
        android.app.AlertDialog.Builder builder = new android.app.AlertDialog.Builder(this, android.R.style.Theme_DeviceDefault_Light_Dialog);
        builder.setTitle("版本更新");
        builder.setMessage("发现新版本 " + versionName + "\\n\\n" + desc);
        builder.setPositiveButton("立即更新", (d, w) -> {
            Intent intent = new Intent(this, com.smartlocker.screen.service.AutoInstallService.class);
            intent.putExtra("download_url", downloadUrl);
            startService(intent);
        });
        if (!force) {
            builder.setNegativeButton("稍后", null);
        }
        builder.setCancelable(!force);
        builder.show();
    }

    private void refreshSlotStatus() {
        String deviceId = mPrefs.getDeviceId();
        if (TextUtils.isEmpty(deviceId)) return;

        String url = mPrefs.getServerUrl() + "/api/cabinets/by-mainboard/" + deviceId;
        try {
            Request request = new Request.Builder()
                    .url(url)
                    .get()
                    .build();

            mOkClient.newCall(request).enqueue(new Callback() {
                @Override
                public void onFailure(Call call, java.io.IOException e) {
                    Log.e(TAG, "refreshSlotStatus failed: " + e.getMessage());
                    runOnUiThread(() -> {
                        if (mSignalView != null) mSignalView.setSignalLevel(0);
                    });
                }

                @Override
                public void onResponse(Call call, Response response) throws java.io.IOException {
                    try {
                        String respStr = response.body().string();
                        JSONObject json = new JSONObject(respStr);
                        if (json.optInt("code") == 200) {
                            JSONObject data = json.optJSONObject("data");
                            if (data != null) {
                                int available = data.optInt("available_slots", 0);
                                int totalSlots = 0;
                                if (data.has("slots")) {
                                    totalSlots = data.getJSONArray("slots").length();
                                }

                                final int avail = available;
                                final int tot = totalSlots;
                                final String usageRules = data.optString("usage_rules", "");
                                final String bizStatus = data.optString("biz_status", "");

                                mDB.updateAvailableBoxes(avail);

                                runOnUiThread(() -> {
                                    if ("inactive".equals(bizStatus) || "closed".equals(bizStatus)) {
                                        displayUsageRules("设备维护中，请移步到隔壁设备");
                                        if (mTvAvailableBoxes != null) mTvAvailableBoxes.setText("维护中");
                                        if (mRunHalfView != null) mRunHalfView.setData(0, tot);
                                        if (mIvQrCode != null) mIvQrCode.setVisibility(View.GONE);
                                    } else {
                                        displayUsageRules(usageRules);
                                        if (mTvAvailableBoxes != null) mTvAvailableBoxes.setText(String.valueOf(avail));
                                        if (mRunHalfView != null) mRunHalfView.setData(avail, tot);
                                        if (mSignalView != null) mSignalView.setSignalLevel(4);
                                    }
                                });
                            }
                        }
                    } catch (Exception e) {
                        Log.e(TAG, "parse refreshSlotStatus: " + e.getMessage());
                    }
                }
            });
        } catch (Exception e) {
            Log.e(TAG, "refreshSlotStatus error: " + e.getMessage());
                                        if (mIvQrCode != null) mIvQrCode.setVisibility(View.VISIBLE);
        }
    }

    private void displayUsageRules(String rules) {
        if (rules == null || rules.trim().isEmpty()) {
            // No rules - hide all guize layouts
            if (mGuize5Include != null) mGuize5Include.setVisibility(View.GONE);
            if (mGuize1Include != null) mGuize1Include.setVisibility(View.GONE);
            return;
        }
        // Parse rules by newline
        rules = rules.replace("\\n", "\n");
        String[] lines = rules.split("\n");
        // Use guize5 layout (simple title + bullet points)
        if (mGuize5Include != null) {
            mGuize5Include.setVisibility(View.VISIBLE);
            try {
                View guizi5 = mGuize5Include.findViewById(R.id.guizi5);
                if (guizi5 != null) {
                    // Set title
                    TextView tvTitle5 = (TextView) guizi5.findViewById(R.id.tv_title5);
                    if (tvTitle5 != null) tvTitle5.setText("寄 存 规 则");
                    // Set bullet points
                    int[] contentIds = {R.id.tv_content5_1, R.id.tv_content5_2, R.id.tv_content5_3, R.id.tv_content5_4, R.id.tv_content5_5};
                    for (int i = 0; i < contentIds.length; i++) {
                        TextView tv = (TextView) guizi5.findViewById(contentIds[i]);
                        if (tv != null) {
                            if (i < lines.length && !lines[i].trim().isEmpty()) {
                                tv.setText(lines[i].trim());
                                tv.setVisibility(View.VISIBLE);
                                // Also make the parent view visible
                                View parent = (View) tv.getParent();
                                if (parent != null) parent.setVisibility(View.VISIBLE);
                            } else {
                                tv.setVisibility(View.GONE);
                                View parent = (View) tv.getParent();
                                if (parent != null) parent.setVisibility(View.GONE);
                            }
                        }
                    }
                }
            } catch (Exception e) {
                Log.e(TAG, "displayUsageRules error: " + e.getMessage());
            }
        }
        // Hide guize1 (not used for dynamic rules)
        if (mGuize1Include != null) mGuize1Include.setVisibility(View.GONE);
    }

    private void showPickupDialog() {
        Intent intent = new Intent(this, PickUpActivity.class);
        startInternalActivity(intent);
    }

    private void pickupByPhoneAndCode(String phone, String code) {
        String url = mPrefs.getServerUrl() + "/api/retrieve/verify";
        try {
            String deviceId = mPrefs.getDeviceId();
            JSONObject body = new JSONObject();
            body.put("phone", phone);
            body.put("access_code", code);
            body.put("device_id", deviceId);

            Request request = new Request.Builder()
                    .url(url)
                    .post(RequestBody.create(body.toString(), okhttp3.MediaType.parse("application/json")))
                    .build();

            mOkClient.newCall(request).enqueue(new Callback() {
                @Override
                public void onFailure(Call call, java.io.IOException e) {
                    runOnUiThread(() -> Toast.makeText(MainActivity.this, "网络错误", Toast.LENGTH_SHORT).show());
                }

                @Override
                public void onResponse(Call call, Response response) throws java.io.IOException {
                    try {
                        String respStr = response.body().string();
                        JSONObject json = new JSONObject(respStr);
                        if (json.optInt("code") == 200) {
                            JSONObject data = json.optJSONObject("data");
                            if (data != null) {
                                int orderId = data.optInt("order_id");
                                int boardNo = data.optInt("board_no", 1);
                                int lockNo = data.optInt("lock_no", 0);
                                if (lockNo <= 0) lockNo = data.optInt("compartment_number", 0);
                                sendLocalOpenLock(boardNo, lockNo);
                                confirmRetrieve(orderId);
                                runOnUiThread(() -> {
                                    Toast.makeText(MainActivity.this, "柜门已打开", Toast.LENGTH_SHORT).show();
                                    refreshSlotStatus();
                                });
                            }
                        } else {
                            String msg = json.optString("message", "验证失败，请检查手机号和取件码");
                            String finalMsg = msg;
                            runOnUiThread(() -> Toast.makeText(MainActivity.this, finalMsg, Toast.LENGTH_SHORT).show());
                        }
                    } catch (Exception e) {
                        runOnUiThread(() -> Toast.makeText(MainActivity.this, "解析错误", Toast.LENGTH_SHORT).show());
                    }
                }
            });
        } catch (Exception e) {
            Log.e(TAG, "pickupByPhoneAndCode error: " + e.getMessage());
        }
    }

    private void sendLocalOpenLock(int boardNo, int lockNo) {
        try {
            String port = mPrefs.getSerialPort();
            int baud = mPrefs.getBaudRate();
            SerialPortManager serial = new SerialPortManager();
            if (!serial.open(port, baud)) {
                Log.e(TAG, "串口打开失败: " + port);
                return;
            }
            String protocol = mPrefs.getProtocolType();
            boolean sent = false;
            if ("QM".equalsIgnoreCase(protocol)) {
                QMProtocol qm = new QMProtocol(serial);
                sent = qm.sendOpenLock(boardNo, lockNo);
            } else if ("WT".equalsIgnoreCase(protocol)) {
                WTProtocol wt = new WTProtocol(serial);
                sent = wt.sendOpenLock(boardNo, lockNo);
            } else {
                YBMProtocol ybm = new YBMProtocol(serial);
                sent = ybm.sendOpenLock(boardNo, lockNo);
            }
            Log.i(TAG, "本地开锁: board=" + boardNo + " lock=" + lockNo + " sent=" + sent);
            new Thread(() -> {
                try { Thread.sleep(1000); } catch (InterruptedException e) {}
                serial.close();
            }).start();
        } catch (Exception e) {
            Log.e(TAG, "sendLocalOpenLock error: " + e.getMessage());
        }
    }

    private void confirmRetrieve(int orderId) {
        String url = mPrefs.getServerUrl() + "/api/retrieve/confirm";
        try {
            JSONObject body = new JSONObject();
            body.put("order_id", orderId);
            body.put("action", "end");

            Request request = new Request.Builder()
                    .url(url)
                    .post(RequestBody.create(body.toString(), okhttp3.MediaType.parse("application/json")))
                    .build();

            mOkClient.newCall(request).enqueue(new Callback() {
                @Override
                public void onFailure(Call call, java.io.IOException e) {
                    Log.e(TAG, "confirmRetrieve failed: " + e.getMessage());
                }

                @Override
                public void onResponse(Call call, Response response) throws java.io.IOException {
                    try {
                        String respStr = response.body().string();
                        Log.i(TAG, "confirmRetrieve: " + respStr);
                    } catch (Exception e) {
                        Log.e(TAG, "confirmRetrieve parse error: " + e.getMessage());
                    }
                }
            });
        } catch (Exception e) {
            Log.e(TAG, "confirmRetrieve error: " + e.getMessage());
        }
    }

    private void startServices() {
        Intent lockerIntent = new Intent(this, LockerService.class);
        startService(lockerIntent);
    }

    private void openAdmin() {
        Intent intent = new Intent(this, AdminActivity.class);
        startInternalActivity(intent);
    }

    public void updateConnectionStatus(boolean connected) {
        runOnUiThread(() -> {
            if (mSignalView != null) {
                mSignalView.setSignalLevel(connected ? 4 : 0);
            }
        });
    }


    private void startInternalActivity(Intent intent) {
        mIsStartingInternal = true;
        startActivity(intent);
    }

    private Handler mRestartHandler = new Handler(Looper.getMainLooper());
    private boolean mIsInBackground = false;
    private boolean mLeaveHintCalled = false;
    private boolean mIsStartingInternal = false;
    public static volatile boolean sSubActivityActive = false;
    private Runnable mRestartRunnable = new Runnable() {
        @Override
        public void run() {
            if (mIsInBackground && !sSubActivityActive) {
                Log.i("MainActivity", "退到桌面10秒，自动重启APP");
                Intent intent = new Intent(MainActivity.this, MainActivity.class);
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
                startActivity(intent);
            }
        }
    };

    @Override
    protected void onUserLeaveHint() {
        super.onUserLeaveHint();
        if (mIsStartingInternal) {
            mIsStartingInternal = false;
            return;
        }
        mLeaveHintCalled = true;
        mIsInBackground = true;
        mRestartHandler.postDelayed(mRestartRunnable, 10000);
        Log.i("MainActivity", "用户退到桌面，10秒后自动重启");
    }

    @Override
    protected void onPause() {
        super.onPause();
        // 如果不是 onUserLeaveHint 触发的 pause（即内部 startActivity），清除重启标记
        if (!mLeaveHintCalled) {
            mRestartHandler.removeCallbacks(mRestartRunnable);
            mIsInBackground = false;
        }
        mLeaveHintCalled = false;
        try {
            if (mRulesReceiver != null) unregisterReceiver(mRulesReceiver);
        } catch (Exception e) {}
    }

    @Override
    protected void onResume() {
        super.onResume();
        mIsInBackground = false;
        refreshSlotStatus();
        mRestartHandler.removeCallbacks(mRestartRunnable);
        hideSystemUI();
        initData();
        // 注册使用规则更新广播
        if (mRulesReceiver == null) {
            mRulesReceiver = new android.content.BroadcastReceiver() {
                @Override
                public void onReceive(android.content.Context context, android.content.Intent intent) {
                    String rules = intent.getStringExtra("usage_rules");
                    Log.i("MainActivity", "收到使用规则更新广播: rules_len=" + (rules != null ? rules.length() : 0));
                    if (rules != null) {
                        displayUsageRules(rules);
                    }
                }
            };
        }
        try {
            unregisterReceiver(mRulesReceiver);
        } catch (Exception e) {}
        registerReceiver(mRulesReceiver, new android.content.IntentFilter("com.smartlocker.USAGE_RULES_UPDATE"));
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        mRefreshHandler.removeCallbacksAndMessages(null);
        if (mStatusChecker != null) { mStatusChecker.removeCallbacksAndMessages(null); }
        if (mWebSocket != null) {
            mWebSocket.close(1000, "activity destroyed");
        }
        try {
            if (mRulesReceiver != null) unregisterReceiver(mRulesReceiver);
        } catch (Exception e) {}
    }
}

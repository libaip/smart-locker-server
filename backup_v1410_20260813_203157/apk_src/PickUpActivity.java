package com.smartlocker.screen;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputFilter;
import android.text.TextUtils;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import com.smartlocker.screen.utils.PreferencesHelper;
import okhttp3.Response;

public class PickUpActivity extends Activity {
    private static final String TAG = "PickUpActivity";
    private static final int AUTO_RETURN_SECONDS = 30; // 输入页面自动返回倒计时
    private static final int SUCCESS_RETURN_SECONDS = 10; // 开门成功页面自动返回倒计时

    private PreferencesHelper mPrefs;
    private OkHttpClient mOkClient;
    private EditText mInputPhone;
    private EditText mInputCode;
    private int mCabinetId = 0;
    private int mAllowMidRetrieve = 1;
    private int mMidRetrieveLimit = -1;
    private int mMidRetrieveRemaining = -1;
    private EditText mFocusedInput;

    // 倒计时相关
    private Handler mHandler = new Handler(Looper.getMainLooper());
    private int mCountdown = AUTO_RETURN_SECONDS;
    private Runnable mCountdownRunnable;
    private TextView mTvCountdown;

    // 开门成功界面
    private LinearLayout mRootView;
    private boolean mIsShowingSuccess = false;
    private LinearLayout mSuccessOverlay;
    // 最后一次开锁数据（用于“再次开门”）
    private int mLastBoardNo = 1;
    private int mLastLockNo = 1;
    private int mLastSlotNo = 0;
    private int mLastCompartmentNo = 0;
    private String mCurrentPhone = "";
    private String mCurrentOrderNo = "";
    private int mCurrentOrderId = 0;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        mPrefs = new PreferencesHelper(this);

        OkHttpClient.Builder okBuilder = new OkHttpClient.Builder()
                .connectTimeout(10, java.util.concurrent.TimeUnit.SECONDS);
        try {
            SSLSocketHelper.trustCustomCert(okBuilder);
        } catch (Exception e) {
            Log.w(TAG, "SSL cert trust: " + e.getMessage());
        }
        mOkClient = okBuilder.build();

        getWindow().setFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN,
                WindowManager.LayoutParams.FLAG_FULLSCREEN);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        // New layout: vertical (top bar | middle content | bottom bar)
        mRootView = new LinearLayout(this);
        mRootView.setOrientation(LinearLayout.VERTICAL);
        mRootView.setBackgroundColor(0xFFF5F5F5);

        // === Top bar: steps (left) + return (right) ===
        LinearLayout topBar = new LinearLayout(this);
        topBar.setOrientation(LinearLayout.HORIZONTAL);
        topBar.setBackgroundColor(0xFF2196F3);
        topBar.setGravity(Gravity.CENTER_VERTICAL);
        topBar.setPadding(dpToPx(24), dpToPx(8), dpToPx(16), dpToPx(8));
        LinearLayout.LayoutParams topBarLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, dpToPx(68));
        topBar.setLayoutParams(topBarLp);

        TextView tvSteps = new TextView(this);
        tvSteps.setText("取包流程：1.输入手机号+密码  2.确认取包用途  3.开门成功");
        tvSteps.setTextSize(22);
        tvSteps.setTextColor(0xFFFFFFFF);
        tvSteps.setLayoutParams(new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1.0f));
        topBar.addView(tvSteps);

        LinearLayout topRight = new LinearLayout(this);
        topRight.setOrientation(LinearLayout.HORIZONTAL);
        topRight.setGravity(Gravity.CENTER_VERTICAL);

        mTvCountdown = new TextView(this);
        mTvCountdown.setTextSize(20);
        mTvCountdown.setTextColor(0xFFFFFFFF);
        mTvCountdown.setPadding(0, 0, dpToPx(10), 0);
        topRight.addView(mTvCountdown);

        Button btnCancel = new Button(this);
        btnCancel.setText("← 返回");
        btnCancel.setTextSize(28);
        btnCancel.setTextColor(0xFFFFFFFF);
        btnCancel.setBackgroundColor(0x00000000);
        btnCancel.setOnClickListener(v -> finish());
        topRight.addView(btnCancel);

        topBar.addView(topRight);
        mRootView.addView(topBar);

        // === Middle: inputs (left) + keyboard (right) ===
        LinearLayout middleArea = new LinearLayout(this);
        middleArea.setOrientation(LinearLayout.HORIZONTAL);
        middleArea.setPadding(dpToPx(30), dpToPx(16), dpToPx(30), dpToPx(16));
        middleArea.setLayoutParams(new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1.0f));

        // === Left: phone + password inputs (large, bold) ===
        LinearLayout inputSide = new LinearLayout(this);
        inputSide.setOrientation(LinearLayout.VERTICAL);
        inputSide.setGravity(Gravity.CENTER);
        inputSide.setPadding(dpToPx(40), dpToPx(20), dpToPx(40), dpToPx(20));
        inputSide.setLayoutParams(new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.MATCH_PARENT, 0.50f));

        TextView tvPickupTitle = new TextView(this);
        tvPickupTitle.setText("取包");
        tvPickupTitle.setTextSize(56);
        tvPickupTitle.setTextColor(0xFFE53935);
        tvPickupTitle.setTypeface(null, android.graphics.Typeface.BOLD);
        tvPickupTitle.setGravity(Gravity.CENTER);
        inputSide.addView(tvPickupTitle);

        TextView tvPickupSub = new TextView(this);
        tvPickupSub.setText("输入手机号+密码");
        tvPickupSub.setTextSize(28);
        tvPickupSub.setTextColor(0xFFE53935);
        tvPickupSub.setGravity(Gravity.CENTER);
        tvPickupSub.setPadding(0, dpToPx(8), 0, dpToPx(24));
        inputSide.addView(tvPickupSub);

        // Phone input
        LinearLayout phoneCard = new LinearLayout(this);
        phoneCard.setOrientation(LinearLayout.VERTICAL);
        phoneCard.setPadding(dpToPx(24), dpToPx(16), dpToPx(24), dpToPx(16));
        phoneCard.setBackgroundColor(0xFFF0F0F0);
        phoneCard.setLayoutParams(new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

        TextView phoneLabel = new TextView(this);
        phoneLabel.setText("手机号码");
        phoneLabel.setTextSize(34);
        phoneLabel.setTextColor(0xFF666666);
        phoneCard.addView(phoneLabel);

        mInputPhone = new EditText(this);
        mInputPhone.setHint("输入存包手机号码");
        mInputPhone.setInputType(android.text.InputType.TYPE_CLASS_PHONE);
        mInputPhone.setTextSize(42);
        mInputPhone.setTextColor(0xFF333333);
        mInputPhone.setHintTextColor(0xFFCCCCCC);
        mInputPhone.setBackgroundColor(0x00000000);
        mInputPhone.setSingleLine(true);
        mInputPhone.setCursorVisible(true);
        mInputPhone.setTypeface(null, android.graphics.Typeface.BOLD);
        mInputPhone.setShowSoftInputOnFocus(false);
        mInputPhone.setLayoutParams(new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        mInputPhone.setOnFocusChangeListener((v, hasFocus) -> {
            if (hasFocus) mFocusedInput = mInputPhone;
        });
        phoneCard.addView(mInputPhone);

        LinearLayout.LayoutParams cardLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        cardLp.setMargins(0, 0, 0, dpToPx(16));
        phoneCard.setLayoutParams(cardLp);
        inputSide.addView(phoneCard);

        // Password input
        LinearLayout codeCard = new LinearLayout(this);
        codeCard.setOrientation(LinearLayout.VERTICAL);
        codeCard.setPadding(dpToPx(24), dpToPx(16), dpToPx(24), dpToPx(16));
        codeCard.setBackgroundColor(0xFFF0F0F0);
        codeCard.setLayoutParams(new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

        TextView codeLabel = new TextView(this);
        codeLabel.setText("取包密码");
        codeLabel.setTextSize(34);
        codeLabel.setTextColor(0xFF666666);
        codeCard.addView(codeLabel);

        mInputCode = new EditText(this);
        mInputCode.setHint("输入取包四位密码");
        mInputCode.setInputType(android.text.InputType.TYPE_CLASS_NUMBER);
        mInputCode.setFilters(new InputFilter[]{ new InputFilter.LengthFilter(4) });
        mInputCode.setTextSize(42);
        mInputCode.setTextColor(0xFF333333);
        mInputCode.setHintTextColor(0xFFCCCCCC);
        mInputCode.setBackgroundColor(0x00000000);
        mInputCode.setSingleLine(true);
        mInputCode.setCursorVisible(true);
        mInputCode.setTypeface(null, android.graphics.Typeface.BOLD);
        mInputCode.setShowSoftInputOnFocus(false);
        mInputCode.setLayoutParams(new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        mInputCode.setOnFocusChangeListener((v, hasFocus) -> {
            if (hasFocus) mFocusedInput = mInputCode;
        });
        codeCard.addView(mInputCode);

        LinearLayout.LayoutParams codeCardLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        codeCardLp.setMargins(0, 0, 0, dpToPx(16));
        codeCard.setLayoutParams(codeCardLp);
        inputSide.addView(codeCard);

        TextView tvForgot = new TextView(this);
        tvForgot.setText("忘记密码？请联系客服");
        tvForgot.setTextSize(24);
        tvForgot.setTextColor(0xFFE53935);
        tvForgot.setPadding(0, dpToPx(8), 0, 0);
        inputSide.addView(tvForgot);

        middleArea.addView(inputSide);

        // === Right: numpad keyboard + confirm ===
        LinearLayout keyboardSide = new LinearLayout(this);
        keyboardSide.setOrientation(LinearLayout.VERTICAL);
        keyboardSide.setGravity(Gravity.CENTER);
        keyboardSide.setLayoutParams(new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.MATCH_PARENT, 0.50f));

        LinearLayout numpadGrid = new LinearLayout(this);
        numpadGrid.setOrientation(LinearLayout.VERTICAL);
        numpadGrid.setGravity(Gravity.CENTER_HORIZONTAL);

        String[] keys = {"1","2","3","4","5","6","7","8","9","","0","del"};
        int keyGap = dpToPx(8);
        for (int row = 0; row < 4; row++) {
            LinearLayout rowLayout = new LinearLayout(this);
            rowLayout.setOrientation(LinearLayout.HORIZONTAL);
            rowLayout.setGravity(Gravity.CENTER);
            LinearLayout.LayoutParams rowLp = new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT, dpToPx(90));
            if (row > 0) rowLp.setMargins(0, keyGap, 0, 0);
            rowLayout.setLayoutParams(rowLp);

            for (int col = 0; col < 3; col++) {
                int idx = row * 3 + col;
                String key = keys[idx];
                Button b = new Button(this);
                if (key.equals("del")) { b.setText("⌫"); b.setTextSize(32); }
                else if (key.equals("")) { b.setText(""); b.setEnabled(false); }
                else { b.setText(key); b.setTextSize(36); }
                b.setTextColor(0xFF333333);
                b.setBackgroundColor(0xFFFFFFFF);
                LinearLayout.LayoutParams btnLp = new LinearLayout.LayoutParams(
                        0, LinearLayout.LayoutParams.MATCH_PARENT, 1.0f);
                if (col > 0) btnLp.setMargins(keyGap, 0, 0, 0);
                b.setLayoutParams(btnLp);
                if (!key.equals("")) b.setOnClickListener(v -> onNumpadClick(key));
                rowLayout.addView(b);
            }
            numpadGrid.addView(rowLayout);
        }
        keyboardSide.addView(numpadGrid);

        Button btnConfirm = new Button(this);
        btnConfirm.setText("确 认");
        btnConfirm.setTextSize(38);
        btnConfirm.setTextColor(0xFFFFFFFF);
        btnConfirm.setBackgroundColor(0xFF2196F3);
        btnConfirm.setLayoutParams(new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, dpToPx(70)));
        btnConfirm.setOnClickListener(v -> doPickup());
        keyboardSide.addView(btnConfirm);

        middleArea.addView(keyboardSide);
        mRootView.addView(middleArea);

        // === Bottom: customer service ===
        LinearLayout bottomBar = new LinearLayout(this);
        bottomBar.setOrientation(LinearLayout.HORIZONTAL);
        bottomBar.setBackgroundColor(0xFF2196F3);
        bottomBar.setGravity(Gravity.CENTER);
        bottomBar.setPadding(0, dpToPx(12), 0, dpToPx(12));
        bottomBar.setLayoutParams(new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, dpToPx(60)));

        TextView tvService = new TextView(this);
        tvService.setText("全国客服电话 4006981080");
        tvService.setTextSize(26);
        tvService.setTextColor(0xFFFFFFFF);
        tvService.setGravity(Gravity.CENTER);
        bottomBar.addView(tvService);

        mRootView.addView(bottomBar);

        setContentView(mRootView);

        // Default focus on phone input
        mFocusedInput = mInputPhone;
        mInputPhone.requestFocus();
        // Hide soft keyboard, use custom numpad
        getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_STATE_ALWAYS_HIDDEN);
        prefetchCabinetId();
        startAutoReturnCountdown();
    }

    private void startAutoReturnCountdown() {
        mCountdown = AUTO_RETURN_SECONDS;
        updateCountdownText();
        if (mCountdownRunnable != null) {
            mHandler.removeCallbacks(mCountdownRunnable);
        }
        mCountdownRunnable = new Runnable() {
            @Override
            public void run() {
                mCountdown--;
                updateCountdownText();
                if (mCountdown <= 0) {
                    if (!mIsShowingSuccess) finish();
                } else {
                    mHandler.postDelayed(this, 1000);
                }
            }
        };
        mHandler.postDelayed(mCountdownRunnable, 1000);
    }

    private void updateCountdownText() {
        if (mTvCountdown != null) {
            mTvCountdown.setText(mCountdown + "秒后自动返回");
        }
    }

    private void resetAutoReturnCountdown() {
        // 用户有操作时重置倒计时（同时重置定时器）
        mCountdown = AUTO_RETURN_SECONDS;
        updateCountdownText();
        if (mCountdownRunnable != null) {
            mHandler.removeCallbacks(mCountdownRunnable);
        }
        mHandler.postDelayed(mCountdownRunnable, 1000);
    }

    @Override
    protected void onStart() {
        super.onStart();
        MainActivity.sSubActivityActive = true;
    }

    @Override
    protected void onStop() {
        super.onStop();
        MainActivity.sSubActivityActive = false;
    }

    @Override
    protected void onResume() {
        super.onResume();
        hideSystemUI();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        if (mCountdownRunnable != null) {
            mHandler.removeCallbacks(mCountdownRunnable);
        }
        mHandler.removeCallbacksAndMessages(null);
    }

    private void hideSystemUI() {
        View decorView = getWindow().getDecorView();
        decorView.setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_FULLSCREEN);
    }

    private int dpToPx(int dp) {
        float density = getResources().getDisplayMetrics().density;
        return (int) (dp * density + 0.5f);
    }

    private void onNumpadClick(String key) {
        // 用户按键操作，重置倒计时
        resetAutoReturnCountdown();

        if (mFocusedInput == null) mFocusedInput = mInputPhone;
        if (key.equals("del")) {
            String text = mFocusedInput.getText().toString();
            if (text.length() > 0) {
                mFocusedInput.setText(text.substring(0, text.length() - 1));
                mFocusedInput.setSelection(mFocusedInput.getText().length());
            }
        } else {
            String current = mFocusedInput.getText().toString();
            if (mFocusedInput == mInputPhone && current.length() >= 11) return;
            if (mFocusedInput == mInputCode && current.length() >= 4) return;
            mFocusedInput.setText(current + key);
            mFocusedInput.setSelection(mFocusedInput.getText().length());
            if (mFocusedInput == mInputPhone && current.length() + 1 >= 11) {
                mInputCode.requestFocus();
                mFocusedInput = mInputCode;
            }
        }
    }

    private void prefetchCabinetId() {
        String deviceId = mPrefs.getDeviceId();
        if (TextUtils.isEmpty(deviceId)) return;
        String cabinetUrl = mPrefs.getServerUrl() + "/api/cabinets/by-mainboard/" + deviceId;
        Request cabinetReq = new Request.Builder().url(cabinetUrl).get().build();
        mOkClient.newCall(cabinetReq).enqueue(new Callback() {
            @Override
            public void onFailure(Call call, java.io.IOException e) {
                Log.e(TAG, "prefetchCabinetId failed: " + e.getMessage());
                    mCabinetId = mPrefs.getCabinetId();
            }

            @Override
            public void onResponse(Call call, Response response) throws java.io.IOException {
                try {
                    String body = response.body().string();
                    JSONObject json = new JSONObject(body);
                    if (json.optInt("code") == 200) {
                        mCabinetId = json.optJSONObject("data").optInt("id", 0);
                        mPrefs.setCabinetId(mCabinetId);
                        mAllowMidRetrieve = json.optJSONObject("data").optInt("allow_mid_retrieve", 1);
                        mMidRetrieveLimit = json.optJSONObject("data").optInt("mid_retrieve_limit", -1);
                        Log.i(TAG, "prefetchCabinetId: " + mCabinetId);
                    }
                } catch (Exception e) {
                    Log.e(TAG, "prefetchCabinetId parse error: " + e.getMessage());
                }
            }
        });
    }

    private void doPickup() {
        String phone = mInputPhone.getText().toString().trim();
        String code = mInputCode.getText().toString().trim();
        if (TextUtils.isEmpty(phone)) {
            Toast.makeText(this, "请输入手机号", Toast.LENGTH_SHORT).show();
            return;
        }
        if (TextUtils.isEmpty(code)) {
            Toast.makeText(this, "请输入取包密码", Toast.LENGTH_SHORT).show();
            return;
        }

        // 停止输入页面的自动返回倒计时
        if (mCountdownRunnable != null) {
            mHandler.removeCallbacks(mCountdownRunnable);
        }

        if (mCabinetId > 0) {
            verifyAndOpen(phone, code, mCabinetId);
        } else {
            String deviceId = mPrefs.getDeviceId();
            String cabinetUrl = mPrefs.getServerUrl() + "/api/cabinets/by-mainboard/" + deviceId;
            Request cabinetReq = new Request.Builder().url(cabinetUrl).get().build();
            mOkClient.newCall(cabinetReq).enqueue(new Callback() {
                @Override
                public void onFailure(Call call, java.io.IOException e) {
                    // 断网时尝试离线取包
                    tryOfflineRetrieve(phone, code);
                }

                @Override
                public void onResponse(Call call, Response response) throws java.io.IOException {
                    try {
                        String body = response.body().string();
                        JSONObject json = new JSONObject(body);
                        int cabId = 0;
                        if (json.optInt("code") == 200) {
                            cabId = json.optJSONObject("data").optInt("id", 0);
                        }
                        if (cabId == 0) {
                            runOnUiThread(() -> {
                                Toast.makeText(PickUpActivity.this, "设备信息获取失败", Toast.LENGTH_SHORT).show();
                                startAutoReturnCountdown();
                            });
                            return;
                        }
                        mCabinetId = cabId;
                        verifyAndOpen(phone, code, cabId);
                    } catch (Exception e) {
                        runOnUiThread(() -> {
                            Toast.makeText(PickUpActivity.this, "解析错误", Toast.LENGTH_SHORT).show();
                            startAutoReturnCountdown();
                        });
                    }
                }
            });
        }
    }

    private void tryOfflineRetrieve(String phone, String code) {
        // 网络失败时先取缓存订单详情用于展示，再走离线取包（保留开锁+记录+同步）
        new Thread(() -> {
            String detailJson = com.smartlocker.screen.service.LockerService.staticGetCachedOrderDetail(phone, code);
            String orderNo = "";
            String compartment = "";
            if (detailJson != null) {
                try {
                    org.json.JSONObject detail = new org.json.JSONObject(detailJson);
                    orderNo = detail.optString("order_no", "");
                    compartment = detail.optString("compartment_number", "");
                } catch (Exception e) {
                    Log.e(TAG, "解析缓存订单详情失败: " + e.getMessage());
                }
            }
            int ok = com.smartlocker.screen.service.LockerService.staticOfflineRetrieve(phone, code);
            if (ok > 0) {
                String finalOrderNo = orderNo;
                String finalCompartment = compartment;
                runOnUiThread(() -> {
                    android.app.AlertDialog.Builder builder = new android.app.AlertDialog.Builder(PickUpActivity.this);
                    builder.setTitle("离线开门成功");
                    String msg = "第" + ok + "号柜门已打开";
                    if (!finalOrderNo.isEmpty()) msg += "\n订单号：" + finalOrderNo;
                    if (!finalCompartment.isEmpty()) msg += "\n柜格号：" + finalCompartment;
                    builder.setMessage(msg);
                    builder.setPositiveButton("再次开门", (dialog, which) -> {
                        mInputCode.setText("");
                        dialog.dismiss();
                    });
                    builder.setNegativeButton("返回首页", (dialog, which) -> finish());
                    builder.setCancelable(false);
                    builder.show();
                    mInputCode.setText("");
                    mIsShowingSuccess = false;
                });
            } else {
                runOnUiThread(() -> {
                    Toast.makeText(PickUpActivity.this, "离线开门失败，请检查手机号和取包密码", Toast.LENGTH_SHORT).show();
                    mInputCode.setText("");
                });
            }
        }).start();
    }

    private void verifyAndOpen(String phone, String code, int cabinetId) {
        String url = mPrefs.getServerUrl() + "/api/retrieve/verify";
        try {
            JSONObject body = new JSONObject();
            body.put("phone", phone);
            body.put("access_code", code);
            body.put("cabinet_id", cabinetId);

            Request request = new Request.Builder()
                    .url(url)
                    .post(RequestBody.create(body.toString(), MediaType.parse("application/json")))
                    .build();

            mOkClient.newCall(request).enqueue(new Callback() {
                @Override
                public void onFailure(Call call, java.io.IOException e) {
                    // 断网时尝试离线取包
                    tryOfflineRetrieve(phone, code);
                }

                @Override
                public void onResponse(Call call, Response response) throws java.io.IOException {
                    try {
                        String respStr = response.body().string();
                        Log.i(TAG, "verifyAndOpen response: " + respStr);
                        JSONObject json = new JSONObject(respStr);
                        if (json.optInt("code") == 200) {
                            JSONObject data = json.optJSONObject("data");
                            if (data != null) {
                                int orderId = data.optInt("order_id");
                                int boardNo = data.optInt("board_no", 1);
                                int lockNo = data.optInt("lock_no", 0);
                                if (lockNo == 0) {
                                    int compartment = data.optInt("compartment_number", 0);
                                    lockNo = compartment;
                                }
                                int finalBoardNo = boardNo;
                                int finalLockNo = lockNo;
                                int finalOrderId = orderId;
                                // 保存当前开锁数据，供“再次开门”使用
                                int slotNumber = data.optInt("slot_number", 0);
                                if (slotNumber <= 0) slotNumber = data.optInt("compartment_number", 0);
                                mLastBoardNo = boardNo;
                                mLastLockNo = lockNo;
                                mLastCompartmentNo = slotNumber;
                                if (slotNumber > 0) mLastSlotNo = slotNumber;
                                mCurrentPhone = phone;
                                mCurrentOrderId = finalOrderId;
                                String finalOrderNo = "";
                                mCurrentOrderNo = finalOrderNo;
                                if (data.has("mid_retrieve_remaining")) {
                                    mMidRetrieveRemaining = data.optInt("mid_retrieve_remaining", -1);
                                }
                                runOnUiThread(() -> showChoiceDialog(finalOrderId, finalBoardNo, finalLockNo, finalOrderNo));
                            }
                        } else {
                            runOnUiThread(() -> {
                                tryOfflineRetrieve(phone, code);
                            });
                        }
                    } catch (Exception e) {
                        Log.e(TAG, "verifyAndOpen parse error: " + e.getMessage());
                        runOnUiThread(() -> {
                            Toast.makeText(PickUpActivity.this, "解析错误", Toast.LENGTH_SHORT).show();
                            startAutoReturnCountdown();
                        });
                    }
                }
            });
        } catch (Exception e) {
            Log.e(TAG, "verifyAndOpen error: " + e.getMessage());
            tryOfflineRetrieve(phone, code);
            startAutoReturnCountdown();
        }
    }

    private void showChoiceDialog(int orderId, int boardNo, int lockNo, String orderNo) {
        // 停止自动返回倒计时，防止弹窗期间被 finish()
        if (mCountdownRunnable != null) {
            mHandler.removeCallbacks(mCountdownRunnable);
        }
        android.app.AlertDialog.Builder builder = new android.app.AlertDialog.Builder(this);
        LinearLayout dialogLayout = new LinearLayout(this);
        dialogLayout.setOrientation(LinearLayout.VERTICAL);
        dialogLayout.setPadding(dpToPx(40), dpToPx(30), dpToPx(40), dpToPx(30));
        dialogLayout.setBackgroundColor(0xFFFFFFFF);

        TextView dialogTitle = new TextView(this);
        dialogTitle.setText("请选择取包方式");
        dialogTitle.setTextSize(30);
        dialogTitle.setTextColor(0xFF333333);
        dialogTitle.setTypeface(null, android.graphics.Typeface.BOLD);
        dialogTitle.setGravity(Gravity.CENTER);
        dialogTitle.setPadding(0, 0, 0, dpToPx(24));
        dialogLayout.addView(dialogTitle);

        final android.app.AlertDialog[] dialogRef = new android.app.AlertDialog[1];
        if (mAllowMidRetrieve == 1 && mMidRetrieveRemaining != 0) {
        Button btnMid = new Button(this);
        btnMid.setText("继续存包（中途取物）");
        btnMid.setTextSize(34);
        btnMid.setTypeface(null, android.graphics.Typeface.BOLD);
        btnMid.setTextColor(0xFFFFFFFF);
        GradientDrawable gdMid = new GradientDrawable();
        gdMid.setCornerRadius(dpToPx(16));
        gdMid.setColors(new int[]{0xFF3B82F6, 0xFF2563EB});
        gdMid.setGradientType(GradientDrawable.LINEAR_GRADIENT);
        gdMid.setOrientation(GradientDrawable.Orientation.LEFT_RIGHT);
        btnMid.setBackground(gdMid);
        btnMid.setLayoutParams(new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, dpToPx(90)));
        btnMid.setOnClickListener(v -> {
            if (dialogRef[0] != null && dialogRef[0].isShowing()) {
                dialogRef[0].dismiss();
            }
            precheckMidRetrieve(orderId, boardNo, lockNo);
        });
        dialogLayout.addView(btnMid);
        }

        Button btnEnd = new Button(this);
        btnEnd.setText("结束寄存（结束订单）");
        btnEnd.setTextSize(34);
        btnEnd.setTypeface(null, android.graphics.Typeface.BOLD);
        btnEnd.setTextColor(0xFFFFFFFF);
        GradientDrawable gdEnd = new GradientDrawable();
        gdEnd.setCornerRadius(dpToPx(16));
        gdEnd.setColors(new int[]{0xFFEF4444, 0xFFDC2626});
        gdEnd.setGradientType(GradientDrawable.LINEAR_GRADIENT);
        gdEnd.setOrientation(GradientDrawable.Orientation.LEFT_RIGHT);
        btnEnd.setBackground(gdEnd);
        LinearLayout.LayoutParams btnEndLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, dpToPx(90));
        btnEndLp.topMargin = dpToPx(16);
        btnEnd.setLayoutParams(btnEndLp);
        btnEnd.setOnClickListener(v -> {
            if (dialogRef[0] != null && dialogRef[0].isShowing()) {
                dialogRef[0].dismiss();
            }
            sendLocalOpenLock(boardNo, lockNo);
            showSuccessPage(lockNo);
            new Thread(() -> {
                try {
                    org.json.JSONObject body = new org.json.JSONObject();
                    body.put("order_id", orderId);
                    body.put("local_opened", true);
                    body.put("device_id", mPrefs.getDeviceId());
                    okhttp3.Request req = new okhttp3.Request.Builder()
                            .url(mPrefs.getServerUrl() + "/api/deposit/end-storage")
                            .post(okhttp3.RequestBody.create(body.toString(), okhttp3.MediaType.parse("application/json"))).build();
                    mOkClient.newCall(req).enqueue(new okhttp3.Callback() {
                        @Override public void onFailure(okhttp3.Call call, java.io.IOException e) { Log.e(TAG, "doEndRetrieve: " + e.getMessage()); }
                        @Override public void onResponse(okhttp3.Call call, okhttp3.Response resp) { resp.close(); }
                    });
                } catch (Exception e) { Log.e(TAG, "doEndRetrieve: " + e.getMessage()); }
            }).start();
        });
        dialogLayout.addView(btnEnd);

        
        dialogRef[0] = builder.create();
        dialogRef[0].setView(dialogLayout, 0, 0, 0, 0);
        dialogRef[0].setCanceledOnTouchOutside(true);
        dialogRef[0].show();
    }

    private void precheckMidRetrieve(int orderId, int boardNo, int lockNo) {
        Toast.makeText(this, "正在校验中途取物次数", Toast.LENGTH_SHORT).show();
        new Thread(() -> {
            try {
                org.json.JSONObject body = new org.json.JSONObject();
                body.put("order_id", orderId);
                okhttp3.Request req = new okhttp3.Request.Builder()
                        .url(mPrefs.getServerUrl() + "/api/deposit/mid-retrieve/precheck")
                        .post(okhttp3.RequestBody.create(body.toString(), okhttp3.MediaType.parse("application/json"))).build();
                mOkClient.newCall(req).enqueue(new okhttp3.Callback() {
                    @Override
                    public void onFailure(okhttp3.Call call, java.io.IOException e) {
                        Log.e(TAG, "precheckMidRetrieve failed: " + e.getMessage());
                        runOnUiThread(() -> Toast.makeText(PickUpActivity.this, "校验失败，请稍后再试", Toast.LENGTH_SHORT).show());
                    }
                    @Override
                    public void onResponse(okhttp3.Call call, okhttp3.Response response) {
                        try {
                            String s = response.body().string();
                            org.json.JSONObject json = new org.json.JSONObject(s);
                            if (json.optInt("code") == 200) {
                                org.json.JSONObject data = json.optJSONObject("data");
                                if (data != null && data.has("mid_retrieve_remaining")) {
                                    mMidRetrieveRemaining = data.optInt("mid_retrieve_remaining", mMidRetrieveRemaining);
                                }
                                runOnUiThread(() -> openLocalAndReportMidRetrieve(orderId, boardNo, lockNo));
                            } else {
                                String msg = json.optString("message", "已超过中途取物次数上限");
                                runOnUiThread(() -> Toast.makeText(PickUpActivity.this, msg, Toast.LENGTH_LONG).show());
                            }
                        } catch (Exception e) {
                            Log.e(TAG, "precheckMidRetrieve parse error: " + e.getMessage());
                            runOnUiThread(() -> Toast.makeText(PickUpActivity.this, "校验失败，请稍后再试", Toast.LENGTH_SHORT).show());
                        }
                    }
                });
            } catch (Exception e) {
                Log.e(TAG, "precheckMidRetrieve error: " + e.getMessage());
                runOnUiThread(() -> Toast.makeText(PickUpActivity.this, "校验失败，请稍后再试", Toast.LENGTH_SHORT).show());
            }
        }).start();
    }

    private void openLocalAndReportMidRetrieve(int orderId, int boardNo, int lockNo) {
        boolean ok = com.smartlocker.screen.service.LockerService.triggerManualOpen(boardNo, lockNo);
        if (ok) {
            showSuccessPage(lockNo);
            reportMidRetrieve(orderId);
        } else {
            try {
                startService(new Intent(this, com.smartlocker.screen.service.LockerService.class));
                mHandler.postDelayed(() -> {
                    boolean ok2 = com.smartlocker.screen.service.LockerService.triggerManualOpen(boardNo, lockNo);
                    if (ok2) {
                        showSuccessPage(lockNo);
                        reportMidRetrieve(orderId);
                    } else {
                        Log.w(TAG, "本地开锁失败: LockerService未就绪");
                        Toast.makeText(PickUpActivity.this, "设备服务未就绪，请稍后再试", Toast.LENGTH_SHORT).show();
                    }
                }, 1500);
            } catch (Exception e2) {
                Log.e(TAG, "启动LockerService失败: " + e2.getMessage());
                Toast.makeText(PickUpActivity.this, "设备服务未就绪，请稍后再试", Toast.LENGTH_SHORT).show();
            }
        }
    }

    private void reportMidRetrieve(int orderId) {
        new Thread(() -> {
            try {
                org.json.JSONObject body = new org.json.JSONObject();
                body.put("order_id", orderId);
                body.put("local_opened", true);
                body.put("device_id", mPrefs.getDeviceId());
                okhttp3.Request req = new okhttp3.Request.Builder()
                        .url(mPrefs.getServerUrl() + "/api/deposit/mid-retrieve")
                        .post(okhttp3.RequestBody.create(body.toString(), okhttp3.MediaType.parse("application/json"))).build();
                mOkClient.newCall(req).enqueue(new okhttp3.Callback() {
                    @Override public void onFailure(okhttp3.Call call, java.io.IOException e) { Log.e(TAG, "reportMidRetrieve: " + e.getMessage()); }
                    @Override public void onResponse(okhttp3.Call call, okhttp3.Response resp) { resp.close(); }
                });
            } catch (Exception e) { Log.e(TAG, "reportMidRetrieve: " + e.getMessage()); }
        }).start();
    }

    private void doMidRetrieve(int orderId, int boardNo, int lockNo) {
        sendLocalOpenLock(boardNo, lockNo);
        String url = mPrefs.getServerUrl() + "/api/deposit/mid-retrieve";
        try {
            org.json.JSONObject body = new org.json.JSONObject();
            body.put("order_id", orderId);
            body.put("local_opened", true);
            body.put("device_id", mPrefs.getDeviceId());
            okhttp3.Request request = new okhttp3.Request.Builder()
                    .url(url).post(okhttp3.RequestBody.create(body.toString(), okhttp3.MediaType.parse("application/json"))).build();
            mOkClient.newCall(request).enqueue(new okhttp3.Callback() {
                @Override public void onFailure(okhttp3.Call call, java.io.IOException e) {
                    Log.e(TAG, "doMidRetrieve failed: " + e.getMessage());
                }
                @Override public void onResponse(okhttp3.Call call, okhttp3.Response response) throws java.io.IOException {
                    runOnUiThread(() -> showSuccessPage(lockNo));
                }
            });
        } catch (Exception e) {
            Log.e(TAG, "doMidRetrieve error: " + e.getMessage());
        }
    }

    private void doEndRetrieve(int orderId, int boardNo, int lockNo) {
        sendLocalOpenLock(boardNo, lockNo);
        String url = mPrefs.getServerUrl() + "/api/deposit/end-storage";
        try {
            org.json.JSONObject body = new org.json.JSONObject();
            body.put("order_id", orderId);
            body.put("local_opened", true);
            body.put("device_id", mPrefs.getDeviceId());
            okhttp3.Request request = new okhttp3.Request.Builder()
                    .url(url).post(okhttp3.RequestBody.create(body.toString(), okhttp3.MediaType.parse("application/json"))).build();
            mOkClient.newCall(request).enqueue(new okhttp3.Callback() {
                @Override public void onFailure(okhttp3.Call call, java.io.IOException e) {
                    Log.e(TAG, "doEndRetrieve failed: " + e.getMessage());
                }
                @Override public void onResponse(okhttp3.Call call, okhttp3.Response response) throws java.io.IOException {
                    runOnUiThread(() -> showSuccessPage(lockNo));
                }
            });
        } catch (Exception e) {
            Log.e(TAG, "doEndRetrieve error: " + e.getMessage());
        }
    }

    /**
     * 开门成功界面 - 覆盖在当前界面之上
     * 显示"开门成功"、柜门号、返回按钮+10秒倒计时
     */
    private void showSuccessPage(int doorNo) {
        mIsShowingSuccess = true;
        if (mCountdownRunnable != null) {
            mHandler.removeCallbacks(mCountdownRunnable);
        }
        // 清除所有可能残留的倒计时消息
        mHandler.removeCallbacksAndMessages(null);
        // 全屏覆盖
        mSuccessOverlay = new LinearLayout(this);
        mSuccessOverlay.setOrientation(LinearLayout.VERTICAL);
        mSuccessOverlay.setGravity(Gravity.CENTER);
        mSuccessOverlay.setBackgroundColor(0xFFF5F5F5);

        // 顶部返回区域（右上角）
        LinearLayout topReturnBar = new LinearLayout(this);
        topReturnBar.setOrientation(LinearLayout.HORIZONTAL);
        topReturnBar.setGravity(Gravity.END | Gravity.CENTER_VERTICAL);
        topReturnBar.setPadding(0, dpToPx(16), dpToPx(24), 0);
        topReturnBar.setLayoutParams(new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, dpToPx(60)));

        Button btnTopReturn = new Button(this);
        btnTopReturn.setText("← 返回");
        btnTopReturn.setTextSize(28);
        btnTopReturn.setTextColor(0xFFE53935);
        btnTopReturn.setBackgroundColor(0x00000000);
        btnTopReturn.setTypeface(null, android.graphics.Typeface.BOLD);
        LinearLayout.LayoutParams topRetLp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, dpToPx(50));
        topRetLp.setMargins(0, 0, dpToPx(16), 0);
        btnTopReturn.setLayoutParams(topRetLp);
        btnTopReturn.setOnClickListener(v -> finish());

        TextView tvTopCountdown = new TextView(this);
        tvTopCountdown.setTextSize(22);
        tvTopCountdown.setTextColor(0xFFE53935);
        tvTopCountdown.setTypeface(null, android.graphics.Typeface.BOLD);
        tvTopCountdown.setLayoutParams(new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, dpToPx(50)));
        topReturnBar.addView(tvTopCountdown);
        topReturnBar.addView(btnTopReturn);
        mSuccessOverlay.addView(topReturnBar);

        // 中间区域：弹性占位，使内容居中
        LinearLayout centerArea = new LinearLayout(this);
        centerArea.setOrientation(LinearLayout.VERTICAL);
        centerArea.setGravity(Gravity.CENTER);
        centerArea.setLayoutParams(new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1.0f));

        // 成功图标
        TextView tvIcon = new TextView(this);
        tvIcon.setText("✅");
        tvIcon.setTextSize(100);
        tvIcon.setGravity(Gravity.CENTER);
        tvIcon.setPadding(0, 0, 0, dpToPx(10));
        centerArea.addView(tvIcon);

        // 成功标题
        TextView tvSuccessTitle = new TextView(this);
        tvSuccessTitle.setText("开门成功");
        tvSuccessTitle.setTextSize(56);
        tvSuccessTitle.setTextColor(0xFF4CAF50);
        tvSuccessTitle.setGravity(Gravity.CENTER);
        tvSuccessTitle.setTypeface(null, android.graphics.Typeface.BOLD);
        tvSuccessTitle.setPadding(0, 0, 0, dpToPx(30));
        centerArea.addView(tvSuccessTitle);

        // 柜门号
        if (doorNo > 0) {
            LinearLayout doorRow = new LinearLayout(this);
            doorRow.setOrientation(LinearLayout.VERTICAL);
            doorRow.setGravity(Gravity.CENTER);
            doorRow.setPadding(0, dpToPx(10), 0, dpToPx(20));

            TextView tvDoorLabel = new TextView(this);
            tvDoorLabel.setText("柜门号");
            tvDoorLabel.setTextSize(32);
            tvDoorLabel.setTextColor(0xFF999999);
            tvDoorLabel.setGravity(Gravity.CENTER);
            doorRow.addView(tvDoorLabel);

            TextView tvDoorNum = new TextView(this);
            int displayNo = mLastCompartmentNo > 0 ? mLastCompartmentNo : doorNo;
            tvDoorNum.setText(String.valueOf(displayNo));
            tvDoorNum.setTextSize(120);
            tvDoorNum.setTextColor(0xFFFF9800);
            tvDoorNum.setTypeface(null, android.graphics.Typeface.BOLD);
            tvDoorNum.setGravity(Gravity.CENTER);
            tvDoorNum.setPadding(0, dpToPx(8), 0, dpToPx(8));
            doorRow.addView(tvDoorNum);

            centerArea.addView(doorRow);
        }

        // 提示文字
        TextView tvTip = new TextView(this);
        tvTip.setText("请取走物品后关好柜门");
        tvTip.setTextSize(26);
        tvTip.setTextColor(0xFF999999);
        tvTip.setGravity(Gravity.CENTER);
        tvTip.setPadding(0, 0, 0, dpToPx(40));
        centerArea.addView(tvTip);

        // 再次开门按钮（大）
        Button btnReopen = new Button(this);
        btnReopen.setText("再次开门");
        btnReopen.setTextSize(36);
        btnReopen.setTypeface(null, android.graphics.Typeface.BOLD);
        btnReopen.setTextColor(0xFFFF9800);
        btnReopen.setBackgroundColor(0xFFFFFFFF);
        android.graphics.drawable.GradientDrawable reopenBg = new android.graphics.drawable.GradientDrawable();
        reopenBg.setColor(0xFFFFFFFF);
        reopenBg.setStroke(dpToPx(3), 0xFFFF9800);
        reopenBg.setCornerRadius(dpToPx(12));
        btnReopen.setBackground(reopenBg);
        LinearLayout.LayoutParams reopenLp = new LinearLayout.LayoutParams(
                dpToPx(300), dpToPx(90));
        btnReopen.setLayoutParams(reopenLp);
        btnReopen.setOnClickListener(v -> {
            if (mCountdownRunnable != null) mHandler.removeCallbacks(mCountdownRunnable);
            if (mLastSlotNo > 0) {
                // 直接开锁，不弹选择框
                sendLocalOpenLock(mLastBoardNo, mLastLockNo);
                showSuccessPage(mLastSlotNo);
            } else {
                doPickup();
            }
        });
        centerArea.addView(btnReopen);

        mSuccessOverlay.addView(centerArea);

        // 底部倒计时
        TextView tvBottomCountdown = new TextView(this);
        tvBottomCountdown.setTextSize(20);
        tvBottomCountdown.setTextColor(0xFF999999);
        tvBottomCountdown.setGravity(Gravity.CENTER);
        tvBottomCountdown.setPadding(0, dpToPx(16), 0, dpToPx(20));
        mSuccessOverlay.addView(tvBottomCountdown);

        // 覆盖层
        FrameLayout overlayContainer = new FrameLayout(this);
        FrameLayout.LayoutParams rootLp = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT);
        // 先移除mRootView的当前父容器，避免IllegalStateException
        if (mRootView.getParent() != null) {
            ((android.view.ViewGroup) mRootView.getParent()).removeView(mRootView);
        }
        overlayContainer.addView(mRootView, rootLp);
        overlayContainer.addView(mSuccessOverlay, rootLp);
        setContentView(overlayContainer);

        // 10秒倒计时
        mCountdown = SUCCESS_RETURN_SECONDS;
        tvTopCountdown.setText(mCountdown + "s");
        tvBottomCountdown.setText(mCountdown + "秒后自动返回首页");
        mCountdownRunnable = new Runnable() {
            @Override
            public void run() {
                mCountdown--;
                if (mCountdown > 0) {
                    tvTopCountdown.setText(mCountdown + "s");
                    tvBottomCountdown.setText(mCountdown + "秒后自动返回首页");
                    mHandler.postDelayed(this, 1000);
                } else {
                    finish();
                }
            }
        };
        mHandler.postDelayed(mCountdownRunnable, 1000);
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
            Log.i(TAG, "本地开锁 board=" + boardNo + " lock=" + lockNo + " sent=" + sent);
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
                    .post(RequestBody.create(body.toString(), MediaType.parse("application/json")))
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
}

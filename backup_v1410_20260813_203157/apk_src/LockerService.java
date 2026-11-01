package com.smartlocker.screen.service;

import android.app.Service;
import android.content.Intent;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.util.Log;
import com.smartlocker.screen.*;
import com.smartlocker.screen.db.DBHelper;
import com.smartlocker.screen.utils.PreferencesHelper;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import okhttp3.*;

/**
 * 后台服务 - 保持设备连接 + 离线取包支持
 * 
 * 功能:
 * - WebSocket连接管理
 * - HTTP轮询（每10秒，作为WebSocket的补充/替代）
 * - 串口通信管理
 * - 心跳机制
 * - 开锁指令处理
 * - 活跃订单缓存（每30秒拉取）
 * - 离线取包验证（本地手机号+密码匹配）
 * - 联网后同步取包记录
 * - 状态上报
 */
public class LockerService extends Service {
    private static final String TAG = "LockerService";
    
    // 心跳间隔
    private static final long HEARTBEAT_INTERVAL = 30000;  // 30秒
    
    // HTTP轮询间隔
    private static final long POLL_INTERVAL = 60; // 60秒
    // 订单缓存间隔
    private static final long CACHE_INTERVAL = 30;  // 30秒
    
    // 单例
    private static LockerService instance;
    private static boolean isConnected = false;
    
    // 去重：记录最近处理的开锁指令，防止WS+HTTP轮询重复执行
    private static volatile String mLastProcessedDoorKey = "";
    private static volatile long mLastProcessedTime = 0;
    private static final long DEDUP_WINDOW_MS = 15000; // 15秒内相同柜门号视为重复
    private static final java.util.concurrent.ConcurrentHashMap<String, Long> mExecutedOrderIds = new java.util.concurrent.ConcurrentHashMap<>();
    private static final long ORDER_DEDUP_WINDOW_MS = 300000; // 5分钟内同一订单不重复执行（仅旧版无cmd_id指令）
    private final java.util.concurrent.ConcurrentHashMap<String, Long> mExecutedCmdIds = new java.util.concurrent.ConcurrentHashMap<>();
    private volatile long mFastPollDeadline = 0;
    private long mLastConnectedTime = 0;
    private boolean mReset4GAttempted = false;
    
    private PreferencesHelper mPrefs;
    private DBHelper mDB;
    private Handler mHandler;
    
    private SerialPortManager mSerialPort;
    private YBMProtocol mYBMProtocol;
    private WTProtocol mWTProtocol;
    private QMProtocol mQMProtocol;
    private LockerWebSocket mWebSocket;
    
    // 当前协议类型: 1=YBM, 2=WT, 3=QM
    private int mCurrentProtocol = YBMProtocol.PROTOCOL_YBM;
    
    private ScheduledExecutorService mScheduledExecutor;
    private ScheduledFuture<?> mPollFuture;
    private ExecutorService mCommandExecutor;
    
    // HTTP客户端
    private OkHttpClient mHttpClient;
    
    // 当前处理的开锁请求
    private volatile String mCurrentOrderId = null;
    private volatile int mCurrentBoardNo = 0;
    private volatile int mCurrentLockNo = 0;
    private volatile String mCurrentCmdId = "";
    private volatile boolean mTasksScheduled = false;
    private volatile boolean mLockResultProcessing = false;  // Bug3修复: 防止竞态条件
    
    // 串口配置重新初始化防抖
    private static final long REINIT_DEBOUNCE_MS = 30000;  // 30秒内只重启一次
    private long mLastReinitTime = 0;
    
    @Override
    public void onCreate() {
        super.onCreate();
        instance = this;
        
        Log.i(TAG, "服务创建");
        
        mPrefs = new PreferencesHelper(this);
        mDB = DBHelper.getInstance(this);
        mHandler = new Handler(Looper.getMainLooper());
        
        mScheduledExecutor = Executors.newScheduledThreadPool(3);
        mCommandExecutor = Executors.newSingleThreadExecutor();
        
        mHttpClient = new OkHttpClient.Builder()
                .connectTimeout(5, TimeUnit.SECONDS)
                .readTimeout(5, TimeUnit.SECONDS)
                .writeTimeout(5, TimeUnit.SECONDS)
                .build();
        
        initComponents();
    }
    
    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        Log.i(TAG, "服务启动");
        
        // 启动前台通知
        startForeground(1, createNotification());
        
        // 防止重复启动任务（服务重启时 onStartCommand 会被多次调用）
        if (mTasksScheduled) {
            Log.i(TAG, "任务已在运行，跳过重复启动");
            return START_STICKY;
        }
        mTasksScheduled = true;

        // 启动组件
        startSerial();
        startWebSocket();
        startHttpPolling();
        startWatchdog();
        // startKeepAlive(); // 已禁用：每15秒startActivity会导致界面反复刷新
        startOrderCache();
        startHeartbeat();
        startOfflineSync();
        
        return START_STICKY;
    }
    
    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
    
    @Override
    public void onDestroy() {
        super.onDestroy();
        Log.i(TAG, "服务销毁");
        
        stopAll();
        instance = null;
    }
    
    // ==================== 组件初始化 ====================
    
    private void initComponents() {
        // 初始化串口协议
        String serialType = mPrefs.getSerialType();
        SerialPortManager.SerialType type;
        if ("NormalSerial".equals(serialType)) {
            type = SerialPortManager.SerialType.NormalSerial;
        } else if ("VioSerial".equals(serialType)) {
            type = SerialPortManager.SerialType.VioSerial;
        } else if ("BaseSerial".equals(serialType)) {
            type = SerialPortManager.SerialType.BaseSerial;
        } else if ("JniSerial".equals(serialType)) {
            type = SerialPortManager.SerialType.JniSerial;
        } else {
            // 默认使用JNI串口，无需File权限检查
            type = SerialPortManager.SerialType.JniSerial;
        }
        
        mSerialPort = new SerialPortManager(type);
        
        String protocolType = mPrefs.getProtocolType();
        if ("WT".equals(protocolType)) {
            mCurrentProtocol = YBMProtocol.PROTOCOL_WT;
            mYBMProtocol = new YBMProtocol(mSerialPort, YBMProtocol.PROTOCOL_WT);
            mWTProtocol = new WTProtocol(mSerialPort);
            mYBMProtocol.setWTProtocol(mWTProtocol);
        } else if ("QM".equals(protocolType)) {
            mCurrentProtocol = YBMProtocol.PROTOCOL_QM;
            mYBMProtocol = new YBMProtocol(mSerialPort, YBMProtocol.PROTOCOL_YBM);
            mQMProtocol = new QMProtocol(mSerialPort);
            mYBMProtocol.setQMProtocol(mQMProtocol);
        } else {
            mCurrentProtocol = YBMProtocol.PROTOCOL_YBM;
            mYBMProtocol = new YBMProtocol(mSerialPort, YBMProtocol.PROTOCOL_YBM);
        }
        
        // 设置主板数组
        mYBMProtocol.setBoardArr(mPrefs.getBoardNoList());
        
        // 设置回调
        mYBMProtocol.setLockCallback(new YBMProtocol.OnLockCallback() {
            @Override
            public void onOpenLockResult(int boardNo, int lockNo, boolean success) {
                Log.i(TAG, "主板回执: board=" + boardNo + ", lock=" + lockNo + ", success=" + success + "（仅诊断，不覆盖开门结果）");
            }
            
            @Override
            public void onMcuIdReceived(int boardNo, String mcuId) {
                Log.i(TAG, "MCU ID: " + mcuId + " (board=" + boardNo + ")");
            }
            
            @Override
            public void onLockStatusReceived(int boardNo, int[] lockStatus) {
                Log.i(TAG, "锁状态: board=" + boardNo + ", count=" + lockStatus.length);
            }
            
            @Override
            public void onError(String message) {
                Log.e(TAG, "协议错误: " + message);
            }
        });
        
        // 初始化WebSocket
        mWebSocket = new LockerWebSocket(mPrefs, this);
        mWebSocket.setListener(new LockerWebSocket.OnWSListener() {
            @Override
            public void onConnected() {
                isConnected = true;
                mLastConnectedTime = System.currentTimeMillis();
                Log.i(TAG, "WebSocket已连接");
                notifyMainActivity(true);
                triggerImmediatePoll();
            }
            
            @Override
            public void onDisconnected() {
                isConnected = false;
                Log.i(TAG, "WebSocket已断开");
                notifyMainActivity(false);
                triggerImmediatePoll();
            }
            
            @Override
            public void onLockCommand(int boardNo, int lockNo, String orderId, String protocol, String cmdId) {
                handleLockCommand(boardNo, lockNo, orderId, protocol, cmdId);
            }
            
            @Override
            public void onOpenAllCommand(String orderId) {
                handleOpenAllCommand(orderId);
            }
            
            @Override
            public void onUpdateCommand(String downloadUrl, String versionName, int versionCode, boolean force, String fileMd5) {
                handleUpdateCommand(downloadUrl, versionName, versionCode, force, fileMd5);
            }
            
            @Override
            public void onUnbindCommand(String message) {
                handleUnbindCommand(message);
            }
            
            @Override
            public void onMessageReceived(String topic, String message) {
                handleServerMessage(topic, message);
            }
            
            @Override
            public void onError(String error) {
                Log.e(TAG, "WebSocket错误: " + error);
            }
        });
    }
    
    // ==================== 串口管理 ====================
    
    private boolean startSerial() {
        String port = mPrefs.getSerialPort();
        int baud = mPrefs.getBaudRate();
        
        boolean success = mSerialPort.open(port, baud);
        
        if (success) {
            mSerialPort.setDataReceivedListener((data, length) -> {
                switch (mCurrentProtocol) {
                    case YBMProtocol.PROTOCOL_WT:
                        if (mWTProtocol != null) mWTProtocol.handleReceivedData(data, length);
                        break;
                    case YBMProtocol.PROTOCOL_QM:
                        if (mQMProtocol != null) mQMProtocol.handleReceivedData(data, length);
                        break;
                    default:
                        mYBMProtocol.handleReceivedData(data, length);
                        break;
                }
            });
            Log.i(TAG, "串口启动成功: " + port + " @ " + baud + ", 协议=" + mCurrentProtocol);
        } else {
            Log.e(TAG, "串口启动失败: " + port);
        }
        
        return success;
    }
    
    private void stopSerial() {
        if (mSerialPort != null) {
            mSerialPort.close();
        }
    }
    
    private void restartSerial() {
        stopSerial();
        mHandler.postDelayed(this::startSerial, 1000);
    }
    
    /**
     * 完全重新初始化串口和协议（用于 update_config 后重建）
     */
    private void reinitSerialAndProtocol() {
        stopSerial();
        
        // 重建 SerialPortManager（可能 SerialType 变了）
        String serialType = mPrefs.getSerialType();
        SerialPortManager.SerialType type;
        if ("NormalSerial".equals(serialType)) {
            type = SerialPortManager.SerialType.NormalSerial;
        } else if ("VioSerial".equals(serialType)) {
            type = SerialPortManager.SerialType.VioSerial;
        } else {
            type = SerialPortManager.SerialType.BaseSerial;
        }
        mSerialPort = new SerialPortManager(type);
        
        // 重建协议对象
        String protocolType = mPrefs.getProtocolType();
        if ("WT".equals(protocolType)) {
            mCurrentProtocol = YBMProtocol.PROTOCOL_WT;
            mYBMProtocol = new YBMProtocol(mSerialPort, YBMProtocol.PROTOCOL_WT);
            mWTProtocol = new WTProtocol(mSerialPort);
            mYBMProtocol.setWTProtocol(mWTProtocol);
        } else if ("QM".equals(protocolType)) {
            mCurrentProtocol = YBMProtocol.PROTOCOL_QM;
            mYBMProtocol = new YBMProtocol(mSerialPort, YBMProtocol.PROTOCOL_YBM);
            mQMProtocol = new QMProtocol(mSerialPort);
            mYBMProtocol.setQMProtocol(mQMProtocol);
        } else {
            mCurrentProtocol = YBMProtocol.PROTOCOL_YBM;
            mYBMProtocol = new YBMProtocol(mSerialPort, YBMProtocol.PROTOCOL_YBM);
        }
        
        // 设置主板数组
        mYBMProtocol.setBoardArr(mPrefs.getBoardNoList());
        
        // 设置回调（与 initComponents 中完全一致）
        mYBMProtocol.setLockCallback(new YBMProtocol.OnLockCallback() {
            @Override
            public void onOpenLockResult(int boardNo, int lockNo, boolean success) {
                Log.i(TAG, "主板回执: board=" + boardNo + ", lock=" + lockNo + ", success=" + success + "（仅诊断，不覆盖开门结果）");
            }
            
            @Override
            public void onMcuIdReceived(int boardNo, String mcuId) {
                Log.i(TAG, "MCU ID: " + mcuId + " (board=" + boardNo + ")");
            }
            
            @Override
            public void onLockStatusReceived(int boardNo, int[] lockStatus) {
                Log.i(TAG, "锁状态: board=" + boardNo + ", count=" + lockStatus.length);
            }
            
            @Override
            public void onError(String message) {
                Log.e(TAG, "协议错误: " + message);
            }
        });
        
        // 延迟启动串口
        mHandler.postDelayed(this::startSerial, 1000);
        
        Log.i(TAG, "reinitSerialAndProtocol完成: serialType=" + serialType 
            + ", protocol=" + protocolType
            + ", port=" + mPrefs.getSerialPort()
            + ", baud=" + mPrefs.getBaudRate());
    }

    // ==================== WebSocket管理 ====================
    
    private void startWebSocket() {
        if (mWebSocket != null) {
            mWebSocket.connect();
        }
    }
    
    private void stopWebSocket() {
        if (mWebSocket != null) {
            mWebSocket.disconnect();
        }
    }
    
    // ==================== HTTP轮询（离线指令兜底） ====================
    
    private void startHttpPolling() {
        scheduleNextPoll(POLL_INTERVAL);
    }

    private void scheduleNextPoll(long delaySeconds) {
        if (mPollFuture != null) {
            mPollFuture.cancel(false);
        }
        mPollFuture = mScheduledExecutor.schedule(() -> {
            boolean hasPending = false;
            try {
                hasPending = pollPendingCommands();
            } catch (Exception e) {
                Log.e(TAG, "HTTP轮询异常: " + e.getMessage());
            }
            scheduleNextPoll(computeNextPollDelay(hasPending));
        }, delaySeconds, TimeUnit.SECONDS);
    }

    private void triggerImmediatePoll() {
        mScheduledExecutor.execute(() -> {
            boolean hasPending = false;
            try {
                hasPending = pollPendingCommands();
            } catch (Exception e) {
                Log.e(TAG, "HTTP立即轮询异常: " + e.getMessage());
            }
            scheduleNextPoll(computeNextPollDelay(hasPending));
        });
    }

    private long computeNextPollDelay(boolean hasPending) {
        long now = System.currentTimeMillis();
        if (hasPending) {
            mFastPollDeadline = now + 3 * 60 * 1000L;
            return 3;
        }
        if (mWebSocket == null || !mWebSocket.isConnected()) {
            if (mFastPollDeadline == 0) {
                mFastPollDeadline = now + 3 * 60 * 1000L;
            }
            if (now < mFastPollDeadline) {
                return 5;
            }
            return 30;
        }
        mFastPollDeadline = 0;
        return POLL_INTERVAL;
    }
    
    private boolean pollPendingCommands() {
        String deviceId = mPrefs.getDeviceId();
        if (deviceId == null || deviceId.isEmpty()) return false;
        
        String url = mPrefs.getServerUrl() + "/api/pending-commands/" + deviceId;
        
        Request request = new Request.Builder()
                .url(url)
                .get()
                .build();
        
        boolean gotCommands = false;
        try (Response response = mHttpClient.newCall(request).execute()) {
            if (!response.isSuccessful()) {
                Log.w(TAG, "轮询失败: " + response.code());
                return false;
            }
            
            String body = response.body().string();
            Log.i(TAG, "HTTP轮询响应: " + body);
            JSONObject json = new JSONObject(body);
            JSONObject data = json.optJSONObject("data");
            if (data == null) data = json;
            
            // 处理积压的开锁指令
            JSONArray commands = data.optJSONArray("commands");
            if (commands != null && commands.length() > 0) {
                gotCommands = true;
                Log.i(TAG, "轮询获取到" + commands.length() + "条待执行指令");
                for (int i = 0; i < commands.length(); i++) {
                    JSONObject cmd = commands.getJSONObject(i);
                    String cmdType = cmd.optString("type", "");
                    if ("force_update".equals(cmdType)) {
                        // 远程更新命令
                        String dlUrl = cmd.optString("download_url", "");
                        String verName = cmd.optString("version_name", "");
                        int verCode = cmd.optInt("version_code", 0);
                        boolean force = cmd.optBoolean("force", false);
                        String fileMd5 = cmd.optString("file_md5", "");
                        if (!dlUrl.isEmpty()) {
                            handleUpdateCommand(dlUrl, verName, verCode, force, fileMd5);
                        }
                    } else if ("usage_rules_update".equals(cmdType)) {
                        // 使用规则更新
                        String rules = cmd.optString("usage_rules", "");
                        if (!rules.isEmpty()) {
                            Intent rulesIntent = new Intent("com.smartlocker.screen.ACTION_UPDATE_RULES");
                            rulesIntent.putExtra("usage_rules", rules);
                            sendBroadcast(rulesIntent);
                        }
                    } else if ("update_config".equals(cmdType)) {
                        // 配置更新命令 - 通过HTTP轮询接收，复用WebSocket的处理逻辑
                        Log.i(TAG, "HTTP轮询收到update_config命令，开始处理");
                        handleServerMessage("update_config", cmd.toString());
                    } else if ("query_lock_status".equals(cmdType)) {
                        handleQueryLockStatus(cmd.toString());
                    } else {
                        // 只处理有明确主板号和锁号的指令，避免误开门
                        int boardNo = cmd.optInt("board_no", 0);
                        int lockNo = cmd.optInt("lock_no", 0);
                        if (boardNo > 0 && lockNo > 0) {
                            String orderId = cmd.optString("order_id", "");
                            String protocol = cmd.optString("protocol", "YBM");
                            String cmdId = cmd.optString("cmd_id", cmd.optString("cmdId", ""));
                            handleLockCommand(boardNo, lockNo, orderId, protocol, cmdId);
                            try { Thread.sleep(300); } catch (Exception ex) {}
                        } else {
                            Log.w(TAG, "跳过无效开门指令: board=" + boardNo + ", lock=" + lockNo);
                        }
                    }
                }
            }
            
            // 检查mainboard_config变化，自动切换串口/协议
            JSONArray mbConfig = data.optJSONArray("mainboard_config");
            if (mbConfig != null && mbConfig.length() > 0) {
                JSONObject first = mbConfig.getJSONObject(0);
                String newPort = first.optString("serial_port", "");
                int newBaud = first.optInt("baud_rate", 0);
                                if (!newPort.isEmpty() && !newPort.startsWith("/dev/")) {
                    newPort = "/dev/" + newPort;
 }
                String newProto = first.optString("protocol", "");
                boolean configChanged = false;
                if (!newPort.isEmpty() && !newPort.equals(mPrefs.getSerialPort())) {
                    Log.i(TAG, "检测到串口变化: " + mPrefs.getSerialPort() + " -> " + newPort);
                    mPrefs.setSerialPort(newPort);
                    configChanged = true;
                }
                if (newBaud > 0 && newBaud != mPrefs.getBaudRate()) {
                    Log.i(TAG, "检测到波特率变化: " + mPrefs.getBaudRate() + " -> " + newBaud);
                    mPrefs.setBaudRate(newBaud);
                    configChanged = true;
                }
                if (!newProto.isEmpty() && !newProto.equals(mPrefs.getProtocolType())) {
                    Log.i(TAG, "检测到协议变化: " + mPrefs.getProtocolType() + " -> " + newProto);
                    mPrefs.setProtocolType(newProto);
                        int boardIndex = first.optInt("board_index", 1);
                        mPrefs.setBoardNo(boardIndex);
                    configChanged = true;
                }
                if (configChanged) {
                    long now = System.currentTimeMillis();
                    if (now - mLastReinitTime < REINIT_DEBOUNCE_MS) {
                        Log.i(TAG, "串口配置变化, 但防抖期内跳过: lastReinit=" + (now - mLastReinitTime) + "ms ago");
                    } else {
                        Log.i(TAG, "配置已变化, 触发串口重新初始化");
                        mLastReinitTime = now;
                        mHandler.post(() -> {
                            stopSerial();
                            mHandler.postDelayed(() -> {
                                initComponents();
                                startSerial();
                            }, 1000);
                        });
                    }
                }
            }

            // 更新活跃订单缓存
            JSONArray orders = data.optJSONArray("orders");
            if (orders != null) {
                updateOrderCache(orders);
            }
            
            // 检查是否有版本更新
            JSONObject updateInfo = data.optJSONObject("update");
            if (updateInfo != null && updateInfo.optBoolean("has_update", false)) {
                String dlUrl = updateInfo.optString("download_url", "");
                String verName = updateInfo.optString("version_name", "");
                int verCode = updateInfo.optInt("version_code", 0);
                boolean force = updateInfo.optBoolean("force", false);
                String fileMd5 = updateInfo.optString("file_md5", "");
                if (!dlUrl.isEmpty()) {
                    handleUpdateCommand(dlUrl, verName, verCode, force, fileMd5);
                }
            }
            
        } catch (Exception e) {
            Log.d(TAG, "轮询请求失败: " + e.getMessage());
            return false;
        }
        return gotCommands;
    }
    
    // ==================== NETWORK_WATCHDOG ====================
    private static final long WATCHDOG_INTERVAL = 60000;
    private static final long RESET_4G_TIMEOUT = 120000;
    private static final long REBOOT_TIMEOUT = 600000;

    private void startWatchdog() {
        mScheduledExecutor.scheduleAtFixedRate(() -> {
            long offlineMs = System.currentTimeMillis() - mLastConnectedTime;
            if (mLastConnectedTime <= 0) return;
            if (offlineMs < 60000) {
                mReset4GAttempted = false;
                return;
            }
            if (offlineMs > REBOOT_TIMEOUT) {
                Log.i(TAG, "Watchdog: 10min offline, reboot");
                try {
                    Runtime.getRuntime().exec("su -c reboot");
                } catch (Exception e) {
                    Log.e(TAG, "Watchdog reboot fail: " + e.getMessage());
                }
            } else if (offlineMs > RESET_4G_TIMEOUT && !mReset4GAttempted) {
                mReset4GAttempted = true;
                Log.i(TAG, "Watchdog: 2min offline, reset 4G");
                new Thread(() -> {
                    try {
                        Runtime.getRuntime().exec("su -c svc data disable");
                        Thread.sleep(3000);
                        Runtime.getRuntime().exec("su -c svc data enable");
                        Log.i(TAG, "Watchdog: 4G reset done");
                    } catch (Exception e) {
                        Log.e(TAG, "Watchdog 4G reset fail: " + e.getMessage());
                    }
                }).start();
            }
        }, WATCHDOG_INTERVAL, WATCHDOG_INTERVAL, java.util.concurrent.TimeUnit.MILLISECONDS);
        Log.i(TAG, "Watchdog started: 2min reset4G, 10min reboot");
    }


    // ==================== 前台保活（已禁用） ====================
    // 已禁用：每15秒startActivity会导致界面反复刷新/重建
    // Android前台服务本身就能保持后台运行，无需强拉Activity
    
    /*
    private static final long KEEP_ALIVE_INTERVAL = 15000;
    
    private void startKeepAlive() {
        mScheduledExecutor.scheduleAtFixedRate(() -> {
            try {
                Intent intent = new Intent(LockerService.this, com.smartlocker.screen.MainActivity.class);
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
                intent.putExtra("keep_alive", true);
                startActivity(intent);
            } catch (Exception e) {
                Log.w(TAG, "前台保活失败: " + e.getMessage());
            }
        }, KEEP_ALIVE_INTERVAL, KEEP_ALIVE_INTERVAL, java.util.concurrent.TimeUnit.MILLISECONDS);
        Log.i(TAG, "前台保活已启动，每15秒检查一次");
    }
    */

    // ==================== 订单缓存（支持离线取包） ====================
    
    private void startOrderCache() {
        // 首次立即拉取
        mScheduledExecutor.schedule(() -> {
            try {
                fetchActiveOrders();
            } catch (Exception e) {
                Log.e(TAG, "订单缓存异常: " + e.getMessage());
            }
        }, 5, TimeUnit.SECONDS);
        
        // 之后每30秒拉取一次
        mScheduledExecutor.scheduleAtFixedRate(() -> {
            try {
                fetchActiveOrders();
            } catch (Exception e) {
                Log.d(TAG, "订单缓存请求失败: " + e.getMessage());
            }
        }, CACHE_INTERVAL, CACHE_INTERVAL, TimeUnit.SECONDS);
        
        // 每5分钟清理过期缓存
        mScheduledExecutor.scheduleAtFixedRate(() -> {
            try {
                mDB.cleanExpiredCache();
                mDB.cleanOldSyncedRecords();
            } catch (Exception e) {
                Log.e(TAG, "清理缓存异常: " + e.getMessage());
            }
        }, 5, 5, TimeUnit.MINUTES);
    }
    
    private void fetchActiveOrders() {
        String deviceId = mPrefs.getDeviceId();
        if (deviceId == null || deviceId.isEmpty()) return;
        
        String url = mPrefs.getServerUrl() + "/api/active-orders/by-device/" + deviceId;
        
        Request request = new Request.Builder()
                .url(url)
                .get()
                .build();
        
        try (Response response = mHttpClient.newCall(request).execute()) {
            if (!response.isSuccessful()) return;
            
            String body = response.body().string();
            Log.i(TAG, "HTTP轮询响应: " + body);
            JSONObject json = new JSONObject(body);
            JSONObject data = json.optJSONObject("data");
            if (data == null) data = json;
            
            JSONArray orders = data.optJSONArray("orders");
            if (orders != null) {
                updateOrderCache(orders);
            }
            
        } catch (Exception e) {
            Log.d(TAG, "获取活跃订单失败: " + e.getMessage());
        }
    }
    
    private void updateOrderCache(JSONArray orders) {
        try {
            List<android.content.ContentValues> orderList = new ArrayList<>();
            for (int i = 0; i < orders.length(); i++) {
                JSONObject o = orders.getJSONObject(i);
                android.content.ContentValues values = new android.content.ContentValues();
                values.put("order_id", o.optInt("order_id"));
                values.put("order_no", o.optString("order_no", ""));
                values.put("user_phone", o.optString("user_phone", ""));
                values.put("access_code", o.optString("access_code", ""));
                values.put("deposit_amount", o.optDouble("deposit_amount", 0));
                values.put("compartment_number", o.optInt("compartment_number", 0));
                values.put("board_no", o.optInt("board_no", 0));
                values.put("lock_no", o.optInt("lock_no", 0));
                values.put("slot_size", o.optString("slot_size", ""));
                values.put("cabinet_id", o.optInt("cabinet_id", 0));
                values.put("store_time", o.optString("store_time", ""));
                orderList.add(values);
            }
            mDB.refreshCachedOrders(orderList, mPrefs.getDeviceId());
            Log.d(TAG, "订单缓存更新: " + orderList.size() + "条活跃订单");
        } catch (Exception e) {
            Log.e(TAG, "更新订单缓存失败: " + e.getMessage());
        }
    }
    
    // ==================== 离线取包验证 ====================
    
    /**
     * 离线取包验证 - 在屏幕端输入手机号+密码
     * 1. 先查本地缓存
     * 2. 匹配成功→开锁+记录到offline_retrieve_records
     * 3. 联网后自动同步到服务器
     */
    public int offlineRetrieve(String phone, String accessCode) {
        // 查本地缓存
        android.content.ContentValues order = mDB.findCachedOrder(phone, accessCode, mPrefs.getDeviceId());
        
        if (order == null) {
            Log.w(TAG, "离线取包验证失败: phone=" + phone);
            return 0;
        }
        
        int orderId = order.getAsInteger("order_id");
        String orderNo = order.getAsString("order_no");
        // 优先使用服务器返回的 board_no/lock_no
        int boardNo = order.containsKey("board_no") ? order.getAsInteger("board_no") : 0;
        int lockNo = order.containsKey("lock_no") ? order.getAsInteger("lock_no") : 0;
        
        // 如果服务器没返回，需要自己计算
        int compartmentNumber = 0;
        if (boardNo <= 0 || lockNo <= 0) {
            // 尝试解析 compartment_number（可能是 "19" 或 "A19" 格式）
            String compartmentStr = order.getAsString("compartment_number");
            if (compartmentStr != null && !compartmentStr.isEmpty()) {
                try {
                    // 尝试直接解析为数字
                    compartmentNumber = Integer.parseInt(compartmentStr);
                } catch (NumberFormatException e) {
                    // 如果是 "A19" 格式，提取数字部分
                    String numPart = compartmentStr.replaceAll("[^0-9]", "");
                    if (!numPart.isEmpty()) {
                        compartmentNumber = Integer.parseInt(numPart);
                    }
                }
            }
            if (compartmentNumber > 0) {
                boardNo = (compartmentNumber - 1) / 16 + 1;
                lockNo = (compartmentNumber - 1) % 16 + 1;
            }
        }
        if (boardNo <= 0 || lockNo <= 0) {
            Log.w(TAG, "离线取包无有效板号/锁号: compartment=" + compartmentNumber);
            return 0;
        }
        
        Log.i(TAG, "离线取包验证成功: orderId=" + orderId + ", compartment=" + compartmentNumber);
        
        // 开锁
        handleLockCommand(boardNo, lockNo, String.valueOf(orderId), mPrefs.getProtocolType(), "offline_" + System.nanoTime());
        
        // 记录到离线取包表
        mDB.recordOfflineRetrieve(orderId, orderNo, phone);
        
        // 尝试立即同步
        // syncOfflineRetrieves();  // 1.2.97: ???????????????
        
        return compartmentNumber;
    }
    
    // ==================== 离线取包同步 ====================
    
    private void startOfflineSync() {
        // 每30秒检查是否有未同步的记录
        mScheduledExecutor.scheduleAtFixedRate(() -> {
            try {
                syncOfflineRetrieves();
            } catch (Exception e) {
                Log.e(TAG, "离线同步异常: " + e.getMessage());
            }
        }, 30, 30, TimeUnit.SECONDS);
    }
    
    private void syncOfflineRetrieves() {
        List<android.content.ContentValues> unsynced = mDB.getUnsyncedRetrieves();
        if (unsynced.isEmpty()) return;
        
        String deviceId = mPrefs.getDeviceId();
        JSONArray records = new JSONArray();
        List<Integer> recordIds = new ArrayList<>();
        
        for (android.content.ContentValues rec : unsynced) {
            try {
                JSONObject r = new JSONObject();
                r.put("order_id", rec.getAsInteger("order_id"));
                r.put("order_no", rec.getAsString("order_no"));
                r.put("user_phone", rec.getAsString("user_phone"));
                r.put("retrieve_time", rec.getAsString("retrieve_time"));
                records.put(r);
                recordIds.add(rec.getAsInteger("_id"));
            } catch (Exception e) {
                Log.e(TAG, "构建同步记录失败: " + e.getMessage());
            }
        }
        
        try {
            JSONObject requestBody = new JSONObject();
            requestBody.put("device_id", deviceId);
            requestBody.put("records", records);
            
            String url = mPrefs.getServerUrl() + "/api/offline-retrieve/mid-retrieve";
            RequestBody body = RequestBody.create(
                    requestBody.toString(),
                    MediaType.parse("application/json"));
            
            Request request = new Request.Builder()
                    .url(url)
                    .post(body)
                    .build();
            
            try (Response response = mHttpClient.newCall(request).execute()) {
                if (response.isSuccessful()) {
                    mDB.markRetrievesSynced(recordIds);
                    Log.i(TAG, "离线取包同步成功: " + recordIds.size() + "条");
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "离线同步请求失败: " + e.getMessage());
        }
    }
    
    // ==================== 心跳机制 ====================
    
    private void startHeartbeat() {
        // 定期发送串口心跳
        mScheduledExecutor.scheduleAtFixedRate(() -> {
            if (mSerialPort != null && mSerialPort.isOpened()) {
                mYBMProtocol.sendHeartbeatAll();
            }
        }, HEARTBEAT_INTERVAL, HEARTBEAT_INTERVAL, TimeUnit.MILLISECONDS);
        
        // 定期发送服务器心跳
        mScheduledExecutor.scheduleAtFixedRate(() -> {
            if (mWebSocket != null && mWebSocket.isConnected()) {
                mWebSocket.sendHeartbeat();
            }
            mPrefs.setLastHeartbeat(System.currentTimeMillis());
        }, HEARTBEAT_INTERVAL, HEARTBEAT_INTERVAL, TimeUnit.MILLISECONDS);
    }
    
    // ==================== 开锁处理 ====================
    
    private void handleLockCommand(int boardNo, int lockNo, String orderId, String protocol, String cmdId) {
        Log.i(TAG, "收到开锁指令: board=" + boardNo + ", lock=" + lockNo + ", orderId=" + orderId);

        if (boardNo <= 0 || lockNo <= 0) {
            Log.w(TAG, "跳过无效开锁指令: board=" + boardNo + ", lock=" + lockNo);
            return;
        }
        
        // 新版指令按cmd_id防重：同一指令重复下发只开一次，用户重试是新编号不会被拦
        final boolean hasCmdId = cmdId != null && !cmdId.isEmpty();
        long now0 = System.currentTimeMillis();
        if (hasCmdId) {
            Long lastCmd = mExecutedCmdIds.get(cmdId);
            if (lastCmd != null && (now0 - lastCmd) < 600000) {
                Log.w(TAG, "指令已执行过，跳过重复开门: cmdId=" + cmdId);
                return;
            }
            mExecutedCmdIds.put(cmdId, now0);
            java.util.Iterator<java.util.Map.Entry<String, Long>> it0 = mExecutedCmdIds.entrySet().iterator();
            while (it0.hasNext()) {
                java.util.Map.Entry<String, Long> e0 = it0.next();
                if ((now0 - e0.getValue()) > 600000) it0.remove();
            }
        }

        // 旧版指令无cmd_id时保留原防重
        final boolean shouldDedup = !hasCmdId && orderId != null && !orderId.isEmpty() && !orderId.startsWith("manual_");
        if (!hasCmdId) {
            if (orderId != null && !orderId.isEmpty() && !orderId.startsWith("manual_")) {
                Long lastExec = mExecutedOrderIds.get(orderId);
                if (lastExec != null && (now0 - lastExec) < ORDER_DEDUP_WINDOW_MS) {
                    Log.w(TAG, "订单已执行过，跳过重复开门: orderId=" + orderId);
                    return;
                }
                mExecutedOrderIds.put(orderId, now0);
                java.util.Iterator<java.util.Map.Entry<String, Long>> it = mExecutedOrderIds.entrySet().iterator();
                while (it.hasNext()) {
                    java.util.Map.Entry<String, Long> entry = it.next();
                    if ((now0 - entry.getValue()) > 600000) it.remove();
                }
            }
            if (shouldDedup) {
                String dedupKey = boardNo + "_" + lockNo;
                if (dedupKey.equals(mLastProcessedDoorKey) && (now0 - mLastProcessedTime) < DEDUP_WINDOW_MS) {
                    Log.w(TAG, "重复开锁指令，跳过: door=" + dedupKey);
                    return;
                }
                mLastProcessedDoorKey = dedupKey;
                mLastProcessedTime = now0;
            }
        }
        
        mCurrentOrderId = orderId;
        mCurrentBoardNo = boardNo;
        mCurrentLockNo = lockNo;
        mCurrentCmdId = cmdId != null ? cmdId : "";
        
        mCommandExecutor.execute(() -> {
            try {
                String targetProtocol = mPrefs.getProtocolType();
                String effectiveProtocol = (protocol != null && !protocol.isEmpty()) ? protocol : targetProtocol;
                
                boolean sent = false;
                
                if ("WT".equalsIgnoreCase(effectiveProtocol) && mWTProtocol != null) {
                    // WT协议：指令写入串口即视为已执行（部分主板不回执，回执超时不算失败）
                    boolean wtSent = mWTProtocol.sendOpenLock(boardNo, lockNo);
                    if (!wtSent) {
                        if (shouldDedup) {
                            mLastProcessedDoorKey = "";
                        }
                        handleLockResult(boardNo, lockNo, false);
                        return;
                    }
                    Thread.sleep(1000);
                    handleLockResult(boardNo, lockNo, true);
                    return;
                } else if ("QM".equalsIgnoreCase(effectiveProtocol) && mQMProtocol != null) {
                    sent = mQMProtocol.sendOpenLock(boardNo, lockNo);
                } else {
                    // 确保YBMProtocol在YBM模式下发送（防止配置切换后内部状态未更新）
                    if (mYBMProtocol.getProtocolType() != YBMProtocol.PROTOCOL_YBM) {
                        mYBMProtocol.setProtocolType(YBMProtocol.PROTOCOL_YBM);
                        Log.i(TAG, "YBMProtocol已切换到YBM模式");
                    }
                    sent = mYBMProtocol.sendOpenLock(boardNo, lockNo);
                }
                
                if (!sent) {
                    if (shouldDedup) {
                        mLastProcessedDoorKey = "";
                    }
                    handleLockResult(boardNo, lockNo, false);
                    return;
                }
                
                Thread.sleep(1000);
                handleLockResult(boardNo, lockNo, true);
                
            } catch (Exception e) {
                Log.e(TAG, "处理开锁失败: " + e.getMessage());
                if (shouldDedup) {
                    mLastProcessedDoorKey = "";
                }
                handleLockResult(boardNo, lockNo, false);
            }
        });
    }
    
    private void handleOpenAllCommand(String orderId) {
        Log.i(TAG, "收到全开指令: orderId=" + orderId);
        
        mCommandExecutor.execute(() -> {
            try {
                int[] boards = mPrefs.getBoardNoList();
                for (int boardNo : boards) {
                    boolean sent = mYBMProtocol.sendOpenAll(boardNo);
                    if (sent) {
                        Thread.sleep(500);
                    }
                }
                
                mDB.recordOpenLock(orderId, 0, 0, 2, true);
                
                if (mWebSocket != null && mWebSocket.isConnected()) {
                    mWebSocket.sendLockResult(orderId, 0, 0, true);
                }
                
            } catch (Exception e) {
                Log.e(TAG, "处理全开失败: " + e.getMessage());
            }
        });
    }
    
    private void handleLockResult(int boardNo, int lockNo, boolean success) {
        // Bug3修复: 防止竞态条件 - 主流程和异步回调同时调用
        if (mLockResultProcessing) {
            Log.w(TAG, "开锁结果正在处理中，跳过重复调用: board=" + boardNo + ", lock=" + lockNo);
            return;
        }
        mLockResultProcessing = true;
        
        String orderId = null;
        String cmdId = null;
        try {
            orderId = mCurrentOrderId;
            cmdId = mCurrentCmdId;
            
            Log.i(TAG, "开锁结果: board=" + boardNo + ", lock=" + lockNo + ", success=" + success);
            
            int openType = orderId != null && !orderId.isEmpty() ? 0 : 1;
            mDB.recordOpenLock(orderId, boardNo, lockNo, openType, success);
            
            mCurrentOrderId = null;
            mCurrentBoardNo = 0;
            mCurrentLockNo = 0;
            mCurrentCmdId = "";
            
            // WebSocket上报
            if (mWebSocket != null && mWebSocket.isConnected()) {
                mWebSocket.sendLockResult(orderId, boardNo, lockNo, success, cmdId == null ? "" : cmdId);
            }
            
            // HTTP上报（兜底，确保服务器收到）
            if (orderId != null && !orderId.isEmpty()) {
                reportLockResultHttp(orderId, boardNo, lockNo, success, cmdId == null ? "" : cmdId);
            }
        } finally {
            mLockResultProcessing = false;
        }
    }
    
    /**
     * 通过HTTP上报开锁结果（WebSocket断线时的兜底）
     */
    private void reportLockResultHttp(String orderId, int boardNo, int lockNo, boolean success, String cmdId) {
        try {
            JSONObject body = new JSONObject();
            body.put("device_id", mPrefs.getDeviceId());
            body.put("order_id", orderId);
            body.put("cmd_id", cmdId == null ? "" : cmdId);
            body.put("board_no", boardNo);
            body.put("lock_no", lockNo);
            body.put("success", success);
            
            String url = mPrefs.getServerUrl() + "/api/device/lock-result";
            RequestBody requestBody = RequestBody.create(
                    body.toString(),
                    MediaType.parse("application/json"));
            
            Request request = new Request.Builder()
                    .url(url)
                    .post(requestBody)
                    .build();
            
            mHttpClient.newCall(request).enqueue(new Callback() {
                @Override
                public void onFailure(Call call, IOException e) {
                    Log.w(TAG, "HTTP上报开锁结果失败: " + e.getMessage());
                }
                
                @Override
                public void onResponse(Call call, Response response) {
                    Log.d(TAG, "HTTP上报开锁结果: " + response.code());
                    response.close();
                }
            });
        } catch (Exception e) {
            Log.e(TAG, "构建HTTP上报失败: " + e.getMessage());
        }
    }
    
    private void handleUpdateCommand(String downloadUrl, String versionName, int versionCode, boolean force) {
        handleUpdateCommand(downloadUrl, versionName, versionCode, force, null);
    }

    private void handleUpdateCommand(String downloadUrl, String versionName, int versionCode, boolean force, String fileMd5) {
        Log.i(TAG, "处理远程更新命令: " + versionName + " url=" + downloadUrl + " md5=" + fileMd5);
        Intent installIntent = new Intent(this, AutoInstallService.class);
        installIntent.putExtra("download_url", downloadUrl);
        installIntent.putExtra("version_name", versionName);
        installIntent.putExtra("version_code", versionCode);
        installIntent.putExtra("force", force);
        if (fileMd5 != null && !fileMd5.isEmpty()) {
            installIntent.putExtra("file_md5", fileMd5);
        }
        startService(installIntent);
        if (mWebSocket != null) {
            try {
                org.json.JSONObject ack = new org.json.JSONObject();
                ack.put("type", "force_update_ack");
                ack.put("device_id", mPrefs.getDeviceId());
                ack.put("status", "downloading");
                ack.put("version_code", versionCode);
                mWebSocket.sendMessage(ack.toString());
            } catch (Exception e) { Log.e(TAG, "发送ack失败: " + e.getMessage()); }
        }
    }
    
    private void handleUnbindCommand(String message) {
        Log.i(TAG, "解绳设备: " + message);
        
        // 清空本地配置
        mPrefs.setDeviceId("");
        
        // 停止WebSocket
        if (mWebSocket != null) {
            mWebSocket.disconnect();
            mWebSocket = null;
        }
        
        // 停止心跳
        // Heartbeat stopped by disconnect()
        
        // 跳转到注册界面
        android.content.Intent intent = new android.content.Intent(this, RegisterActivity.class);
        intent.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK | android.content.Intent.FLAG_ACTIVITY_CLEAR_TASK);
        startActivity(intent);
    }
    
    private void handleServerMessage(String topic, String message) {
        try {
            JSONObject json = new JSONObject(message);
            String type = json.optString("type", "");
            
            switch (type) {
                case "update_config":
                    boolean needReinit = false;
                    if (json.has("serial_port")) {
                        mPrefs.setSerialPort(json.getString("serial_port"));
                    }
                    if (json.has("baud_rate")) {
                        mPrefs.setBaudRate(json.getInt("baud_rate"));
                    }
                    if (json.has("serial_type")) {
                        mPrefs.setSerialType(json.getString("serial_type"));
                        needReinit = true;
                    }
                    if (json.has("protocol_type")) {
                        mPrefs.setProtocolType(json.getString("protocol_type"));
                        needReinit = true;
                    }
                    if (needReinit) {
                        // 协议类型或串口类型变了，需要完全重新初始化
                        stopSerial();
                        mHandler.postDelayed(() -> {
                            initComponents();
                            startSerial();
                        }, 1000);
                    } else {
                        restartSerial();
                    }
                    break;
                    
                case "query_status":
                    reportDeviceStatus();
                    break;
                    
                case "query_door_status":
                    handleQueryDoorStatus(json);
                    break;
                    
                case "usage_rules_update":
                    handleUsageRulesUpdate(json);
                    break;
                    
                default:
                    Log.d(TAG, "收到消息: " + topic + " -> " + message);
            }
        } catch (Exception e) {
            Log.e(TAG, "处理消息失败: " + e.getMessage());
        }
    }
    
    // ==================== 状态上报 ====================
    
    private void handleQueryDoorStatus(JSONObject json) {
        try {
            String requestId = json.optString("request_id", "");
            int boardNo = json.optInt("board_no", json.optInt("boardNo", 1));
            int lockNo = json.optInt("lock_no", json.optInt("lockNo", 1));
            String protocol = json.optString("protocol", "YBM");
            
            Log.i(TAG, "收到查询柜门开关状态: board=" + boardNo + " lock=" + lockNo + " protocol=" + protocol);
            
            boolean querySuccess = false;
            
            if ("QM".equalsIgnoreCase(protocol) && mQMProtocol != null) {
                querySuccess = mQMProtocol.sendReadStatus(boardNo, lockNo);
                try { Thread.sleep(800); } catch (InterruptedException e) {}
            } else if ("WT".equalsIgnoreCase(protocol) && mWTProtocol != null) {
                mWTProtocol.clearLastLockStatus();
                querySuccess = mWTProtocol.sendReadStatus(boardNo);
                try { Thread.sleep(800); } catch (InterruptedException e) {}
            } else if (mYBMProtocol != null) {
                mYBMProtocol.clearLastLockStatus();
                querySuccess = mYBMProtocol.sendReadLockStatus(boardNo);
                try { Thread.sleep(800); } catch (InterruptedException e) {}
            }
            
            // 从协议层获取真实状态
            boolean isOpen = false;
            if (querySuccess) {
                int[] status = null;
                if ("WT".equalsIgnoreCase(protocol) && mWTProtocol != null) {
                    status = mWTProtocol.getLastLockStatus();
                } else if (mYBMProtocol != null) {
                    status = mYBMProtocol.getLastLockStatus();
                }
                
                if (status != null && lockNo >= 1 && lockNo <= status.length) {
                    isOpen = status[lockNo - 1] == 1;
                    Log.i(TAG, "柜门真实状态: board=" + boardNo + " lock=" + lockNo + " isOpen=" + isOpen);
                } else {
                    Log.w(TAG, "锁状态数据为空或锁号越界: lockNo=" + lockNo + " statusLen=" + (status != null ? status.length : 0));
                }
            }
            
            // Send response back to server
            if (mWebSocket != null && !requestId.isEmpty()) {
                JSONObject resp = new JSONObject();
                resp.put("type", "door_status_result");
                resp.put("request_id", requestId);
                resp.put("device_id", mPrefs.getDeviceId());
                resp.put("board_no", boardNo);
                resp.put("lock_no", lockNo);
                resp.put("is_open", isOpen);
                resp.put("door_status", isOpen ? "open" : "closed");
                resp.put("query_success", querySuccess);
                mWebSocket.sendMessage(resp.toString());
                Log.i(TAG, "发送柜门状态响应: is_open=" + isOpen + " request_id=" + requestId);
            }
        } catch (Exception e) {
            Log.e(TAG, "查询柜门状态失败: " + e.getMessage());
        }
    }

    private void handleQueryLockStatus(String cmdJson) {
        try {
            JSONObject json = new JSONObject(cmdJson);
            String requestId = json.optString("request_id", "");
            int boardNo = json.optInt("board_no", json.optInt("boardNo", 1));
            int lockNo = json.optInt("lock_no", json.optInt("lockNo", 1));
            String protocol = json.optString("protocol", "YBM");
            Log.i(TAG, "收到轮询查询柜门状态: board=" + boardNo + " lock=" + lockNo + " protocol=" + protocol);

            boolean querySuccess = false;
            if ("QM".equalsIgnoreCase(protocol) && mQMProtocol != null) {
                querySuccess = mQMProtocol.sendReadStatus(boardNo, lockNo);
                try { Thread.sleep(800); } catch (InterruptedException e) {}
            } else if ("WT".equalsIgnoreCase(protocol) && mWTProtocol != null) {
                mWTProtocol.clearLastLockStatus();
                querySuccess = mWTProtocol.sendReadStatus(boardNo);
                try { Thread.sleep(800); } catch (InterruptedException e) {}
            } else if (mYBMProtocol != null) {
                mYBMProtocol.clearLastLockStatus();
                querySuccess = mYBMProtocol.sendReadLockStatus(boardNo);
                try { Thread.sleep(800); } catch (InterruptedException e) {}
            }

            boolean isOpen = false;
            if (querySuccess) {
                int[] status = null;
                if ("WT".equalsIgnoreCase(protocol) && mWTProtocol != null) {
                    status = mWTProtocol.getLastLockStatus();
                } else if (mYBMProtocol != null) {
                    status = mYBMProtocol.getLastLockStatus();
                }
                if (status != null && lockNo >= 1 && lockNo <= status.length) {
                    isOpen = status[lockNo - 1] == 1;
                } else {
                    Log.w(TAG, "锁状态数据为空或锁号越界: lockNo=" + lockNo);
                }
            }

            JSONObject resp = new JSONObject();
            resp.put("request_id", requestId);
            resp.put("device_id", mPrefs.getDeviceId());
            resp.put("board_no", boardNo);
            resp.put("lock_no", lockNo);
            resp.put("is_open", isOpen);
            resp.put("door_status", isOpen ? "open" : "closed");
            resp.put("status", querySuccess ? "ok" : "read_failed");
            resp.put("query_success", querySuccess);
            postLockStatusResult(resp.toString());
        } catch (Exception e) {
            Log.e(TAG, "handleQueryLockStatus失败: " + e.getMessage());
        }
    }

    private void postLockStatusResult(String jsonBody) {
        try {
            String url = mPrefs.getServerUrl() + "/api/device/lock-status-report";
            RequestBody body = RequestBody.create(jsonBody, MediaType.parse("application/json; charset=utf-8"));
            Request request = new Request.Builder().url(url).post(body).build();
            mHttpClient.newCall(request).enqueue(new okhttp3.Callback() {
                @Override
                public void onFailure(okhttp3.Call call, java.io.IOException e) {
                    Log.e(TAG, "postLockStatusResult失败: " + e.getMessage());
                }
                @Override
                public void onResponse(okhttp3.Call call, okhttp3.Response response) throws java.io.IOException {
                    response.close();
                }
            });
        } catch (Exception e) {
            Log.e(TAG, "postLockStatusResult: " + e.getMessage());
        }
    }

    private void handleUsageRulesUpdate(JSONObject json) {
        try {
            String rules = json.optString("usage_rules", "");
            Log.i(TAG, "收到使用规则更新: rules_len=" + rules.length());
            Intent intent = new Intent("com.smartlocker.USAGE_RULES_UPDATE");
            intent.putExtra("usage_rules", rules);
            sendBroadcast(intent);
        } catch (Exception e) {
            Log.e(TAG, "处理使用规则更新失败: " + e.getMessage());
        }
    }

        private void reportDeviceStatus() {
        if (mWebSocket == null || !mWebSocket.isConnected()) {
            return;
        }
        
        try {
            JSONObject status = new JSONObject();
            status.put("deviceId", mPrefs.getDeviceId());
            status.put("serialPort", mPrefs.getSerialPort());
            status.put("baudRate", mPrefs.getBaudRate());
            status.put("protocol", mPrefs.getProtocolType());
            status.put("boardCount", mPrefs.getBoardCount());
            status.put("online", true);
            status.put("timestamp", System.currentTimeMillis());
            
            Log.d(TAG, "上报状态: " + status.toString());
        } catch (Exception e) {
            Log.e(TAG, "上报状态失败: " + e.getMessage());
        }
    }
    
    // ==================== 通知主界面 ====================
    
    private void notifyMainActivity(boolean connected) {
        Intent intent = new Intent("com.smartlocker.CONNECTION_CHANGED");
        intent.putExtra("connected", connected);
        sendBroadcast(intent);
    }
    
    // ==================== 服务控制 ====================
    
    private void stopAll() {
        stopSerial();
        stopWebSocket();
        
        if (mScheduledExecutor != null) {
            mTasksScheduled = false;
            mScheduledExecutor.shutdown();
        }
        if (mCommandExecutor != null) {
            mCommandExecutor.shutdown();
        }
        
        isConnected = false;
    }
    
    private android.app.Notification createNotification() {
        android.app.Notification.Builder builder;
        
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
            builder = new android.app.Notification.Builder(this, SmartLockerApp.CHANNEL_ID);
        } else {
            builder = new android.app.Notification.Builder(this);
        }
        
        return builder
                .setContentTitle("智能寄存柜运行中")
                .setContentText("设备: " + mPrefs.getDeviceId())
                .setSmallIcon(R.drawable.ic_notification)
                .setPriority(android.app.Notification.PRIORITY_LOW)
                .setOngoing(true)
                .build();
    }
    
    // ==================== 公共接口 ====================
    
    public static boolean isServiceRunning() {
        return instance != null;
    }
    
    public static boolean isConnected() {
        return isConnected;
    }
    
    public static boolean triggerManualOpen(int boardNo, int lockNo) {
        if (instance != null) {
            String protocol = instance.mPrefs.getProtocolType();
            instance.handleLockCommand(boardNo, lockNo, "manual_" + boardNo + "_" + lockNo, protocol, "");
            return true;
        }
        return false;
    }
    
    public static void triggerOpenAll() {
        if (instance != null) {
            instance.handleOpenAllCommand("openall_" + System.currentTimeMillis());
        }
    }
    
    /**
     * 离线取包 - 公共入口
     * 在MainActivity的取包对话框中调用
     */
    public static int staticOfflineRetrieve(String phone, String accessCode) {
        if (instance != null) {
            return instance.offlineRetrieve(phone, accessCode);
        }
        return 0;
    }

    public static String staticGetCachedOrderDetail(String phone, String accessCode) {
        if (instance != null) {
            return instance.getCachedOrderDetail(phone, accessCode);
        }
        return null;
    }

    private String getCachedOrderDetail(String phone, String accessCode) {
        try {
            android.content.ContentValues order = mDB.findCachedOrder(phone, accessCode, mPrefs.getDeviceId());
            if (order == null) return null;
            JSONObject j = new JSONObject();
            Integer orderId = order.getAsInteger("order_id");
            j.put("order_id", orderId != null ? orderId.intValue() : 0);
            String orderNo = order.getAsString("order_no");
            j.put("order_no", orderNo == null ? "" : orderNo);
            Integer board = order.containsKey("board_no") ? order.getAsInteger("board_no") : null;
            j.put("board_no", board != null ? board.intValue() : 0);
            Integer lock = order.containsKey("lock_no") ? order.getAsInteger("lock_no") : null;
            j.put("lock_no", lock != null ? lock.intValue() : 0);
            String compartment = order.getAsString("compartment_number");
            j.put("compartment_number", compartment == null ? "" : compartment);
            return j.toString();
        } catch (Exception e) {
            Log.e(TAG, "getCachedOrderDetail失败: " + e.getMessage());
            return null;
        }
    }
    
    
    /**
     * 获取缓存订单数量（供UI显示）
     */
    public static int getCachedOrderCount() {
        if (instance != null) {
            return instance.mDB.getValidCachedOrders().size();
        }
        return 0;
    }
    
    /**
     * 获取未同步记录数量（供UI显示）
     */
    public static int getUnsyncedCount() {
        if (instance != null) {
            return instance.mDB.getUnsyncedRetrieves().size();
        }
        return 0;
    }
    
    // ==================== ????? ====================
    
    /**
     * ??????????
     * @param boardNo ??? (1-based)
     * @return ???16?int???0=??1=??null=????
     */

}

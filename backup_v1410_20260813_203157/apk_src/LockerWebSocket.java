package com.smartlocker.screen;

import android.util.Log;
import com.google.gson.Gson;
import com.smartlocker.screen.utils.PreferencesHelper;
import okhttp3.*;
import org.json.JSONObject;
import android.content.Context;
import java.util.concurrent.TimeUnit;

/**
 * WebSocket客户端 - 基于原生OkHttp WebSocket
 * 
 * 与ws_middleware.py对接，使用纯JSON协议，snake_case字段名
 * 协议：
 * - 连接: ws://locker.cqdyxl.com/ws/?device_id=xxx
 * - 注册: {"type":"register","device_id":"xxx","version":"1.2.0","deviceType":"LOCKER_SCREEN"}
 * - 心跳: {"type":"heartbeat","device_id":"xxx","timestamp":123,"status":"online"}
 * - 心跳应答: {"type":"heartbeat_ack"}
 * - 开锁指令: {"type":"open_lock","board_no":1,"lock_no":1,"order_id":"xxx","protocol":"YBM"}
 * - 开锁结果: {"type":"lock_result","order_id":"xxx","device_id":"xxx","board_no":1,"lock_no":1,"success":true,"timestamp":123}
 * - 全开指令: {"type":"open_all","order_id":"xxx"}
 */
public class LockerWebSocket {
    private static final String TAG = "LockerWS";
    
    private WebSocket mWebSocket;
    private OkHttpClient mHttpClient;
    private PreferencesHelper mPrefs;
    private Gson mGson;
    
    private String mDeviceId;
    private boolean mConnected = false;
    
    // 重连相关
    private int mReconnectDelay = 1000;
    private int mMaxReconnectDelay = 30000;
    private boolean mShouldReconnect = true;
    private Thread mReconnectThread;
    
    // 心跳相关
    private static final long HEARTBEAT_INTERVAL = 25000;  // 25秒，匹配ws_middleware超时
    private Thread mHeartbeatThread;
    
    // 回调接口
    private OnWSListener mListener;
    private Context mContext;
    
    public interface OnWSListener {
        void onConnected();
        void onDisconnected();
        void onLockCommand(int boardNo, int lockNo, String orderId, String protocol, String cmdId);
        void onOpenAllCommand(String orderId);
        void onUpdateCommand(String downloadUrl, String versionName, int versionCode, boolean force, String fileMd5);
        void onMessageReceived(String topic, String message);
        void onUnbindCommand(String message);
        void onError(String error);
    }
    
    public void setListener(OnWSListener listener) {
        mListener = listener;
    }
    
    public LockerWebSocket(PreferencesHelper prefs, Context context) {
        this.mPrefs = prefs;
        this.mContext = context;
        this.mGson = new Gson();
        this.mDeviceId = prefs.getDeviceId();
        
        OkHttpClient.Builder wsBuilder = new OkHttpClient.Builder()
                .connectTimeout(10, TimeUnit.SECONDS)
                .readTimeout(0, TimeUnit.MINUTES)
                .writeTimeout(10, TimeUnit.SECONDS)
                .pingInterval(0, TimeUnit.SECONDS);
        SSLSocketHelper.trustCustomCert(wsBuilder);
        this.mHttpClient = wsBuilder.build();
    }
    
    /**
     * 连接WebSocket服务器
     */
    public void connect() {
        if (mConnected && mWebSocket != null) {
            Log.w(TAG, "已经连接");
            return;
        }
        
        String wsUrl = mPrefs.getWebSocketUrl();
        // 强制使用wss://（SSLSocketHelper已配置自定义证书信任）
        // 20260712: 工业屏SSL握手有问题，直接走ws://
        // if (wsUrl.startsWith("ws://")) {
        //     wsUrl = "wss://" + wsUrl.substring(5);
        // }
        // 确保使用 /ws/ 路径
        if (!wsUrl.contains("/ws")) {
            wsUrl = "ws://locker.cqdyxl.com/ws/";
        }
        String fullUrl = wsUrl + "?device_id=" + mDeviceId;
        
        Log.i(TAG, "正在连接: " + fullUrl);
        
        try {
            Request request = new Request.Builder()
                    .url(fullUrl)
                    .build();
            
            mWebSocket = mHttpClient.newWebSocket(request, new WebSocketListener() {
                @Override
                public void onOpen(WebSocket webSocket, Response response) {
                    Log.i(TAG, "WebSocket连接成功");
                    mConnected = true;
                    mReconnectDelay = 1000;
                    
                    // 连接成功后注册设备
                    sendDeviceRegister();
                    
                    // 启动心跳
                    startHeartbeat();
                    
                    if (mListener != null) {
                        mListener.onConnected();
                    }
                }
                
                @Override
                public void onMessage(WebSocket webSocket, String text) {
                    handleMessage(text);
                }
                
                @Override
                public void onClosing(WebSocket webSocket, int code, String reason) {
                    Log.w(TAG, "WebSocket正在关闭: code=" + code + " reason=" + reason);
                    webSocket.close(1000, null);
                }
                
                @Override
                public void onClosed(WebSocket webSocket, int code, String reason) {
                    Log.w(TAG, "WebSocket已关闭: code=" + code + " reason=" + reason);
                    handleDisconnect();
                }
                
                @Override
                public void onFailure(WebSocket webSocket, Throwable t, Response response) {
                    Log.e(TAG, "WebSocket连接失败: " + t.getMessage());
                    handleDisconnect();
                }
            });
            
        } catch (Exception e) {
            Log.e(TAG, "连接异常: " + e.getMessage());
            if (mListener != null) {
                mListener.onError("连接异常: " + e.getMessage());
            }
            scheduleReconnect();
        }
    }
    
    /**
     * 处理收到的消息
     */
    private void handleMessage(String text) {
        try {
            JSONObject json = new JSONObject(text);
            String type = json.optString("type", "");
            
            Log.d(TAG, "收到消息: type=" + type);
            
            switch (type) {
                case "heartbeat_ack":
                    // 心跳应答，无需处理
                    Log.d(TAG, "心跳应答收到");
                    break;
                    
                case "register_ack":
                    Log.i(TAG, "注册确认: " + text);
                    break;
                    
                case "open_lock":
                    handleOpenLock(json);
                    break;
                    
                case "open_all":
                    handleOpenAll(json);
                    break;
                    
                case "force_update":
                    handleForceUpdate(json);
                    break;
                    
                case "update_config":
                case "query_status":
                case "query_door_status":
                    if (mListener != null) {
                        mListener.onMessageReceived(type, text);
                    }
                    break;
                    
                                case "unbind":
                    handleUnbind(json);
                    break;
                    
                default:
                    Log.d(TAG, "未知消息类型: " + type);
                    if (mListener != null) {
                        mListener.onMessageReceived(type, text);
                    }
            }
        } catch (Exception e) {
            Log.e(TAG, "解析消息失败: " + e.getMessage() + " raw=" + text);
        }
    }
    
    /**
     * 处理开锁指令
     * 兼容 snake_case 和 camelCase 字段名
     */
    private void handleOpenLock(JSONObject json) {
        try {
            // snake_case (ws_middleware.py发送) 和 camelCase (旧socket.io格式) 兼容
            int boardNo = json.optInt("board_no", json.optInt("boardNo", 1));
            int lockNo = json.optInt("lock_no", json.optInt("lockNo", 1));
            String orderId = json.optString("order_id", json.optString("orderId", ""));
            String protocol = json.optString("protocol", "YBM");
            String cmdId = json.optString("cmd_id", json.optString("cmdId", ""));
            
            Log.i(TAG, "收到开锁指令: board=" + boardNo + " lock=" + lockNo + " orderId=" + orderId + " cmdId=" + cmdId);
            
            if (mListener != null) {
                mListener.onLockCommand(boardNo, lockNo, orderId, protocol, cmdId);
            }
        } catch (Exception e) {
            Log.e(TAG, "解析开锁指令失败: " + e.getMessage());
        }
    }
    
    /**
     * 处理全开指令
     */
    private void handleOpenAll(JSONObject json) {
        try {
            String orderId = json.optString("order_id", json.optString("orderId", ""));
            Log.i(TAG, "收到全开指令: orderId=" + orderId);
            
            if (mListener != null) {
                mListener.onOpenAllCommand(orderId);
            }
        } catch (Exception e) {
            Log.e(TAG, "解析全开指令失败: " + e.getMessage());
        }
    }
    
    /**
     * 处理强制更新
     */
    private void handleUnbind(JSONObject json) {
        String msg = json.optString("message", "");
        Log.i(TAG, "解绳指令: " + msg);
        if (mListener != null) {
            mListener.onUnbindCommand(msg);
        }
    }
    
    private void handleForceUpdate(JSONObject json) {
        try {
            String downloadUrl = json.optString("download_url", json.optString("downloadUrl", ""));
            String versionName = json.optString("version_name", json.optString("versionName", ""));
            int versionCode = json.optInt("version_code", json.optInt("versionCode", 0));
            boolean force = json.optBoolean("force", true);
            String fileMd5 = json.optString("file_md5", "");
            Log.i(TAG, "收到远程更新: " + versionName + " vc=" + versionCode + " url=" + downloadUrl + " md5=" + fileMd5);
            if (mListener != null) {
                mListener.onUpdateCommand(downloadUrl, versionName, versionCode, force, fileMd5);
            }
        } catch (Exception e) {
            Log.e(TAG, "解析force_update消息失败: " + e.getMessage());
        }
    }
    
    /**
     * 处理断开连接
     */
    private void handleDisconnect() {
        boolean wasConnected = mConnected;
        mConnected = false;
        stopHeartbeat();
        
        if (mListener != null) {
            mListener.onDisconnected();
        }
        
        if (mShouldReconnect) {
            scheduleReconnect();
        }
    }
    
    /**
     * 断开连接
     */
    public void disconnect() {
        mShouldReconnect = false;
        stopHeartbeat();
        
        if (mReconnectThread != null) {
            mReconnectThread.interrupt();
            mReconnectThread = null;
        }
        
        if (mWebSocket != null) {
            mWebSocket.close(1000, "client disconnect");
            mWebSocket = null;
        }
        
        mConnected = false;
    }
    
    /**
     * 发送设备注册 - 使用 snake_case 与 ws_middleware.py 对接
     */
    public void sendDeviceRegister() {
        if (mWebSocket == null || !mConnected) {
            Log.w(TAG, "未连接，无法注册设备");
            return;
        }
        
        try {
            JSONObject data = new JSONObject();
            data.put("type", "register");
            data.put("device_id", mDeviceId);
            try { android.content.pm.PackageInfo pi = mContext.getPackageManager().getPackageInfo(mContext.getPackageName(), 0); data.put("version", pi.versionName); data.put("version_code", pi.versionCode); } catch(Exception e) { data.put("version", "unknown"); }
            data.put("deviceType", "LOCKER_SCREEN");
            
            mWebSocket.send(data.toString());
            Log.i(TAG, "发送设备注册: " + mDeviceId);
            
        } catch (Exception e) {
            Log.e(TAG, "构建注册消息失败: " + e.getMessage());
        }
    }
    
    /**
     * 发送心跳 - 使用 snake_case 与 ws_middleware.py 对接
     */
    public void sendHeartbeat() {
        if (mWebSocket == null || !mConnected) {
            return;
        }
        
        try {
            JSONObject data = new JSONObject();
            data.put("type", "heartbeat");
            data.put("device_id", mDeviceId);
            data.put("timestamp", System.currentTimeMillis());
            data.put("status", "online");
            
            mWebSocket.send(data.toString());
            Log.d(TAG, "发送心跳: " + mDeviceId);
            
        } catch (Exception e) {
            Log.e(TAG, "构建心跳失败: " + e.getMessage());
        }
    }
    
    /**
     * 发送开锁结果 - 使用 snake_case 与 ws_middleware.py 对接
     */
    public boolean sendMessage(String message) { if (mWebSocket == null || !mConnected) { return false; } try { mWebSocket.send(message); return true; } catch (Exception e) { return false; } }
    public String getDeviceId() { return mDeviceId; }
    public void sendLockResult(String orderId, int boardNo, int lockNo, boolean success) {
        sendLockResult(orderId, boardNo, lockNo, success, "");
    }

    public void sendLockResult(String orderId, int boardNo, int lockNo, boolean success, String cmdId) {
        if (mWebSocket == null || !mConnected) {
            Log.w(TAG, "未连接，无法发送结果");
            return;
        }
        
        try {
            JSONObject data = new JSONObject();
            data.put("type", "lock_result");
            data.put("order_id", orderId);
            data.put("device_id", mDeviceId);
            data.put("board_no", boardNo);
            data.put("lock_no", lockNo);
            data.put("success", success);
            data.put("timestamp", System.currentTimeMillis());
            if (cmdId != null && !cmdId.isEmpty()) {
                data.put("cmd_id", cmdId);
                data.put("cmdId", cmdId);
            }
            
            mWebSocket.send(data.toString());
            Log.i(TAG, "发送开锁结果: orderId=" + orderId + " success=" + success);
            
        } catch (Exception e) {
            Log.e(TAG, "构建结果消息失败: " + e.getMessage());
        }
    }
    
    /**
     * 启动心跳线程
     */
    private void startHeartbeat() {
        stopHeartbeat();
        mHeartbeatThread = new Thread(() -> {
            while (mConnected && mHeartbeatThread == Thread.currentThread()) {
                try {
                    Thread.sleep(HEARTBEAT_INTERVAL);
                    sendHeartbeat();
                } catch (InterruptedException e) {
                    break;
                }
            }
        });
        mHeartbeatThread.setDaemon(true);
        mHeartbeatThread.start();
    }
    
    /**
     * 停止心跳
     */
    private void stopHeartbeat() {
        if (mHeartbeatThread != null) {
            mHeartbeatThread.interrupt();
            mHeartbeatThread = null;
        }
    }
    
    /**
     * 自动重连
     */
    private void scheduleReconnect() {
        if (mReconnectThread != null && mReconnectThread.isAlive()) {
            return;  // 已有重连任务
        }
        
        mReconnectThread = new Thread(() -> {
            try {
                Thread.sleep(mReconnectDelay);
                // 指数退避
                mReconnectDelay = Math.min(mReconnectDelay * 2, mMaxReconnectDelay);
                
                Log.i(TAG, "尝试重连...");
                connect();
                
            } catch (InterruptedException e) {
                // 被中断，不重连
            }
        });
        mReconnectThread.setDaemon(true);
        mReconnectThread.start();
    }
    
    /**
     * 检查连接状态
     */
    public boolean isConnected() {
        return mConnected;
    }
    
    public boolean isWebSocketConnected() {
        return mConnected;
    }
    
    public boolean isStompConnected() {
        return mConnected;
    }
    
    // 兼容方法
    public void subscribe(String destination) {
        Log.d(TAG, "subscribe（原生WebSocket无需订阅）: " + destination);
    }
    
    public void subscribeAll() {
        Log.d(TAG, "subscribeAll（原生WebSocket无需订阅）");
    }
}

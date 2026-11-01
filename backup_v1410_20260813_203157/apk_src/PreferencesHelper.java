package com.smartlocker.screen.utils;

import android.content.Context;
import android.content.SharedPreferences;

/**
 * SharedPreferences工具类
 * 存储设备配置参数
 */
public class PreferencesHelper {
    private static final String PREF_NAME = "locker_config";
    
    // 服务器配置
    private static final String KEY_SERVER_URL = "server_url";
    private static final String KEY_WEBSOCKET_URL = "websocket_url";
    
    // 设备配置
    private static final String KEY_DEVICE_ID = "device_id";
    private static final String KEY_DEVICE_NAME = "device_name";
    private static final String KEY_STORE_NAME = "store_name";
    
    // 串口配置
    private static final String KEY_SERIAL_PORT = "serial_port";
    private static final String KEY_BAUD_RATE = "baud_rate";
    private static final String KEY_SERIAL_TYPE = "serial_type";
    
    // 主板配置
    private static final String KEY_BOARD_NO = "board_no";
    private static final String KEY_CABINET_ID = "cabinet_id";
    private static final String KEY_BOARD_COUNT = "board_count";
    private static final String KEY_PROTOCOL_TYPE = "protocol_type";
    
    // 管理员配置
    private static final String KEY_ADMIN_PASSWORD = "admin_password";
    
    // 状态
    private static final String KEY_IS_REGISTERED = "is_registered";
    private static final String KEY_LAST_HEARTBEAT = "last_heartbeat";
    private static final String KEY_AUTH_TOKEN = "auth_token";
    
    // 默认值
    private static final String DEFAULT_SERVER = "http://locker.cqdyxl.com";
    private static final String DEFAULT_WEBSOCKET = "ws://locker.cqdyxl.com/ws2/";
    private static final String DEFAULT_SERIAL_PORT = "/dev/ttyS1";
    private static final int DEFAULT_BAUD_RATE = 9600;
    private static final String DEFAULT_PROTOCOL = "YBM";
    
    private final SharedPreferences mPrefs;
    
    public PreferencesHelper(Context context) {
        mPrefs = context.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE);
    }
    
    // ==================== 服务器配置 ====================
    
    public String getServerUrl() {
        return mPrefs.getString(KEY_SERVER_URL, DEFAULT_SERVER);
    }
    
    public void setServerUrl(String url) {
        mPrefs.edit().putString(KEY_SERVER_URL, url).apply();
    }
    
    public String getWebSocketUrl() {
        return mPrefs.getString(KEY_WEBSOCKET_URL, DEFAULT_WEBSOCKET);
    }
    
    public void setWebSocketUrl(String url) {
        mPrefs.edit().putString(KEY_WEBSOCKET_URL, url).apply();
    }
    
    // ==================== 设备配置 ====================
    
    public String getDeviceId() {
        return mPrefs.getString(KEY_DEVICE_ID, "");
    }
    
    public void setDeviceId(String id) {
        mPrefs.edit().putString(KEY_DEVICE_ID, id).apply();
    }
    
    public static String getDeviceId(Context context) {
        return context.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE)
                .getString(KEY_DEVICE_ID, "");
    }
    
    public String getDeviceName() {
        return mPrefs.getString(KEY_DEVICE_NAME, "");
    }
    
    public void setDeviceName(String name) {
        mPrefs.edit().putString(KEY_DEVICE_NAME, name).apply();
    }
    
    public String getStoreName() {
        return mPrefs.getString(KEY_STORE_NAME, "");
    }
    
    public void setStoreName(String name) {
        mPrefs.edit().putString(KEY_STORE_NAME, name).apply();
    }
    
    // ==================== 串口配置 ====================
    
    public String getSerialPort() {
        return mPrefs.getString(KEY_SERIAL_PORT, DEFAULT_SERIAL_PORT);
    }
    
    public void setSerialPort(String port) {
        mPrefs.edit().putString(KEY_SERIAL_PORT, port).apply();
    }
    
    public int getBaudRate() {
        return mPrefs.getInt(KEY_BAUD_RATE, DEFAULT_BAUD_RATE);
    }
    
    public void setBaudRate(int rate) {
        mPrefs.edit().putInt(KEY_BAUD_RATE, rate).apply();
    }
    
    public String getSerialType() {
        return mPrefs.getString(KEY_SERIAL_TYPE, "BaseSerial");
    }
    
    public void setSerialType(String type) {
        mPrefs.edit().putString(KEY_SERIAL_TYPE, type).apply();
    }
    
    // ==================== 主板配置 ====================
    
    public int getBoardNo() {
        return mPrefs.getInt(KEY_BOARD_NO, 1);
    }
    
    public void setBoardNo(int no) {
        mPrefs.edit().putInt(KEY_BOARD_NO, no).apply();
    }
    
    public int getBoardCount() {
        return mPrefs.getInt(KEY_BOARD_COUNT, 1);
    }
    
    public void setBoardCount(int count) {
        mPrefs.edit().putInt(KEY_BOARD_COUNT, count).apply();
    }
    
    public String getProtocolType() {
        return mPrefs.getString(KEY_PROTOCOL_TYPE, DEFAULT_PROTOCOL);
    }
    
    public void setProtocolType(String type) {
        mPrefs.edit().putString(KEY_PROTOCOL_TYPE, type).apply();
    }
    
    // ==================== 管理员配置 ====================
    
    public String getAdminPassword() {
        return mPrefs.getString(KEY_ADMIN_PASSWORD, "123456");
    }
    
    public void setAdminPassword(String password) {
        mPrefs.edit().putString(KEY_ADMIN_PASSWORD, password).apply();
    }
    
    // ==================== 状态 ====================
    
    public boolean isRegistered() {
        return mPrefs.getBoolean(KEY_IS_REGISTERED, false);
    }
    
    public void setRegistered(boolean registered) {
        mPrefs.edit().putBoolean(KEY_IS_REGISTERED, registered).apply();
    }
    
    public long getLastHeartbeat() {
        return mPrefs.getLong(KEY_LAST_HEARTBEAT, 0);
    }
    
    public void setLastHeartbeat(long time) {
        mPrefs.edit().putLong(KEY_LAST_HEARTBEAT, time).apply();
    }
    
    public String getAuthToken() {
        return mPrefs.getString(KEY_AUTH_TOKEN, "");
    }
    
    public void setAuthToken(String token) {
        mPrefs.edit().putString(KEY_AUTH_TOKEN, token).apply();
    }
    
    /**
     * 获取所有主板编号列表
     */
    public int[] getBoardNoList() {
        int count = getBoardCount();
        int[] boards = new int[count];
        int startNo = getBoardNo();
        for (int i = 0; i < count; i++) {
            boards[i] = startNo + i;
        }
        return boards;
    }
    
    /**
     * 保存主板编号列表
     */
    public void setBoardNoList(int startNo, int count) {
        mPrefs.edit()
                .putInt(KEY_BOARD_NO, startNo)
                .putInt(KEY_BOARD_COUNT, count)
                .apply();
    }
    
    /**
     * 清除所有配置
     */
    public void clearAll() {
        mPrefs.edit().clear().apply();
    }
    public int getCabinetId() { return mPrefs.getInt(KEY_CABINET_ID, 0); }
    public void setCabinetId(int cabinetId) { mPrefs.edit().putInt(KEY_CABINET_ID, cabinetId).apply(); }

    public int getLockCount() { return mPrefs.getInt("lock_count", 16); }
}

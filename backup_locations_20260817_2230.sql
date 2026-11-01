--
-- PostgreSQL database dump
--

\restrict XgjOnfPHwqnusRksfDuIGhiZbcE6F20ZcW11IETaOBGdooXcv8VrggMEdY43LVP

-- Dumped from database version 14.23 (Ubuntu 14.23-0ubuntu0.22.04.1)
-- Dumped by pg_dump version 14.23 (Ubuntu 14.23-0ubuntu0.22.04.1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: locations; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE public.locations (
    id integer NOT NULL,
    merchant_id integer,
    name text NOT NULL,
    address text,
    longitude double precision,
    latitude double precision,
    status integer DEFAULT 1,
    created_at timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    withdraw_enabled integer DEFAULT 1,
    auto_approve_day integer DEFAULT 1,
    auto_approve_time text DEFAULT '12:00'::text,
    auto_approve_rate double precision DEFAULT 80,
    click_free_count integer DEFAULT 3,
    anti_test_minutes integer DEFAULT 30,
    anti_test_auto_refund integer DEFAULT 1,
    hide_ratio integer DEFAULT 0,
    whitelist_phones text DEFAULT ''::text,
    duplicate_filter_enabled integer DEFAULT 0,
    duplicate_filter_days integer DEFAULT 7,
    duplicate_filter_limit integer DEFAULT 3,
    show_refunding_status text DEFAULT 1,
    refund_approve_rate double precision DEFAULT 80,
    refund_approve_start_min integer DEFAULT 60,
    refund_approve_end_min integer DEFAULT 300,
    contact_phone text DEFAULT ''::text,
    open_time text DEFAULT '08:00'::text,
    close_time text DEFAULT '22:00'::text,
    allow_slot_select integer DEFAULT 0,
    slot_assign_mode text DEFAULT 'auto'::text,
    allow_mid_retrieve integer DEFAULT 1,
    retrieve_mode text DEFAULT 'both'::text,
    allow_h5_to_mp integer DEFAULT 0,
    show_qr_follow integer DEFAULT 0,
    force_follow_mp integer DEFAULT 0,
    h5_url text DEFAULT ''::text,
    show_slot_count integer DEFAULT 1,
    screen_show_title integer DEFAULT 1,
    screen_title text DEFAULT ''::text,
    slot_full_alert integer DEFAULT 0,
    slot_full_text text DEFAULT ''::text,
    end_alert_minutes integer DEFAULT 0,
    enable_clear_box integer DEFAULT 1,
    clear_box_time text DEFAULT '03:00'::text,
    clear_box_cycle integer DEFAULT 1,
    deposit_random integer DEFAULT 0,
    deposit_min double precision DEFAULT 0,
    deposit_max double precision DEFAULT 0,
    contact_name text DEFAULT ''::text,
    refund_mode text DEFAULT 'auto_approve'::text,
    withdraw_mode text DEFAULT 'manual_approve'::text,
    province text DEFAULT ''::text,
    agent_id integer,
    city text DEFAULT ''::text,
    usage_rules text DEFAULT ''::text,
    deposit_amount double precision DEFAULT 20.0,
    mp_appid text DEFAULT ''::text,
    balance_hide_enabled integer DEFAULT 0,
    balance_hide_days integer DEFAULT 30,
    last_clear_date date,
    legacy_visible_orders integer DEFAULT 0,
    charge_mode character varying(50),
    show_deposit_fields boolean DEFAULT false,
    allow_open_after_end integer DEFAULT 1,
    mid_retrieve_limit integer,
    hide_start_orders integer DEFAULT 50 NOT NULL,
    rules_title text,
    reopen_times integer,
    reject_whitelist_after integer DEFAULT 1
);


ALTER TABLE public.locations OWNER TO postgres;

--
-- Name: locations_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE public.locations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER TABLE public.locations_id_seq OWNER TO postgres;

--
-- Name: locations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE public.locations_id_seq OWNED BY public.locations.id;


--
-- Name: locations id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.locations ALTER COLUMN id SET DEFAULT nextval('public.locations_id_seq'::regclass);


--
-- Data for Name: locations; Type: TABLE DATA; Schema: public; Owner: postgres
--

COPY public.locations (id, merchant_id, name, address, longitude, latitude, status, created_at, withdraw_enabled, auto_approve_day, auto_approve_time, auto_approve_rate, click_free_count, anti_test_minutes, anti_test_auto_refund, hide_ratio, whitelist_phones, duplicate_filter_enabled, duplicate_filter_days, duplicate_filter_limit, show_refunding_status, refund_approve_rate, refund_approve_start_min, refund_approve_end_min, contact_phone, open_time, close_time, allow_slot_select, slot_assign_mode, allow_mid_retrieve, retrieve_mode, allow_h5_to_mp, show_qr_follow, force_follow_mp, h5_url, show_slot_count, screen_show_title, screen_title, slot_full_alert, slot_full_text, end_alert_minutes, enable_clear_box, clear_box_time, clear_box_cycle, deposit_random, deposit_min, deposit_max, contact_name, refund_mode, withdraw_mode, province, agent_id, city, usage_rules, deposit_amount, mp_appid, balance_hide_enabled, balance_hide_days, last_clear_date, legacy_visible_orders, charge_mode, show_deposit_fields, allow_open_after_end, mid_retrieve_limit, hide_start_orders, rules_title, reopen_times, reject_whitelist_after) FROM stdin;
7	1	亿航驾校		\N	\N	1	2026-06-29 17:43:30.813267	1	3	02:00	0	1	5	1	30		0	7	3	1	80	60	300	13549878542	08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	23:00	1	0	0	0	wang	auto_approve	manual_approve		\N			20	wx57eaea52dcfff4e8	1	30	2026-08-17	0	\N	f	1	0	50	\N	\N	1
6	4	淮海考场	河南商丘市	\N	\N	1	2026-06-25 09:24:57	1	3	02:02	0	1	30	1	30	\N	0	7	3	1	80	60	300	139000000000	08:00	22:00	0	auto	1	both	1	0	0	\N	0	1	\N	0	\N	0	1	23:00	1	0	0	0	李	auto_approve	manual_approve	\N	\N	\N	\N	20	wx57eaea52dcfff4e8	0	30	2026-08-17	0	\N	f	1	\N	40	\N	\N	1
13	10	宜阳科目一		\N	\N	1	2026-07-03 10:09:09.957627	1	1	02:00	0	1	30	1	15		0	7	3	1	80	60	300		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	0	03:00	1	0	0	0		auto_approve	manual_approve		\N			20		0	30	\N	0	\N	f	1	\N	30	\N	\N	1
14	11	新安考场		\N	\N	1	2026-07-05 09:05:49.380868	1	1	12:00	0	1	30	0	26		0	7	3	1	80	60	300		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	0	03:00	1	0	0	0		auto_approve	manual_approve		\N			20		0	30	\N	0	\N	f	1	\N	50	\N	\N	1
23	21	喜德和锐考场		\N	\N	1	2026-07-30 13:42:38.99192	1	1	12:00	80	0	30	0	15		0	7	3	1	30	0	0		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	03:00	1	0	0	0		auto_approve	queue_approve		\N			20		0	30	2026-08-17	0	\N	f	1	\N	50	\N	\N	1
16	12	柘城科目一		\N	\N	1	2026-07-05 09:06:26.005741	1	1	12:00	80	1	30	1	0		0	7	3	1	80	60	300		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	23:00	1	0	0	0		auto_approve	manual_approve		\N			20		0	30	2026-08-17	0	\N	f	1	\N	50	\N	\N	1
11	8	偃师考场		\N	\N	1	2026-07-02 22:02:52.267989	1	3	02:00	0	1	5	0	35	13155553469	0	7	3	1	40	2	10		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	23:00	1	0	0	0		auto_approve	manual_approve		\N			20		0	30	2026-08-17	0	\N	f	1	\N	50	\N	\N	1
8	5	誉荣科目三		\N	\N	1	2026-07-02 21:21:43.884414	1	3	02:00	0	1	5	1	30		0	7	3	1	80	60	300		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	23:00	1	0	0	0		auto_approve	manual_approve		\N			20		0	30	2026-08-17	4662	\N	f	1	10	50	\N	\N	1
10	7	宁陵科目三		\N	\N	1	2026-07-02 21:42:43.646023	1	3	02:00	0	1	30	0	35		0	7	3	0	80	60	300		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	23:00	1	0	0	0		auto_approve	manual_approve		\N			20	wx57eaea52dcfff4e8	0	30	2026-08-17	0	\N	f	1	\N	20	\N	\N	1
27	25	金马科目一		\N	\N	1	2026-08-03 07:18:53.778526	1	1	12:00	80	0	30	1	0		0	7	3	1	80	60	300		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	03:00	1	0	0	0		auto_approve	auto_approve		\N		可以免费中途开门一次\n但十分钟内必须放进去\n客服电话：13251385780	20		0	30	2026-08-17	0	\N	f	1	1	50	\N	\N	1
24	22	金马科目三		\N	\N	1	2026-07-31 16:06:53.418434	1	1	12:00	80	0	30	0	0		0	7	3	1	80	60	300		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	03:00	1	0	0	0		auto_approve	auto_approve		\N		可以免费中途开门一次\n但十分钟内必须放进去\n客服电话：13251385780	20		0	30	2026-08-17	0	\N	f	1	1	50	\N	1	1
17	15	太安考场		\N	\N	1	2026-07-05 09:08:17.259408	1	1	12:00	0	1	30	1	0		0	7	3	1	80	60	300		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	0	03:00	1	0	0	0		auto_approve	manual_approve		\N			20		0	30	\N	0	\N	f	1	\N	50	\N	\N	1
19	17	湄潭考场		\N	\N	1	2026-07-14 23:20:47.244307	1	3	12:00	100	1	5	1	20		0	7	3	1	50	5	60		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	03:00	1	0	0	0		auto_approve	queue_approve		\N			20		0	30	2026-08-17	0	\N	f	1	\N	50	\N	\N	1
4	1	测试网点_修改测试	新地址	\N	\N	1	2026-06-12 05:12:41	1	1	12:00	0	0	30	0	30	13900139000	0	7	3	1	80	1	1	\N	08:00	22:00	0	auto	1	both	1	0	0	\N	0	1	\N	0	\N	0	0	03:00	1	0	0	0	1895588779	auto_approve	queue_approve	\N	\N	\N		20	wx57eaea52dcfff4e8	1	30	\N	0	\N	f	0	\N	50	\N	\N	1
18	16	九龙考场		\N	\N	1	2026-07-14 11:38:47.105185	1	3	00:00	0	1	5	1	35		0	7	3	1	80	60	300	123456	06:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	03:00	1	0	0	0	11	auto_approve	manual_approve		\N			20		0	30	2026-08-17	0	\N	f	1	\N	10	\N	\N	1
35	33	茂靠考场		\N	\N	1	2026-08-11 14:36:39.643967	1	1	12:00	80	0	30	0	15		0	7	3	1	50	0	0		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	03:00	1	0	0	0		auto_approve	queue_approve		\N			20		0	30	2026-08-17	0	\N	f	1	\N	50	\N	\N	1
31	29	亨泰考场		\N	\N	1	2026-08-08 15:54:11.604154	1	1	12:00	80	0	30	0	15		0	7	3	1	80	60	300		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	03:00	1	0	0	0		auto_approve	auto_approve		\N			20		0	30	2026-08-17	0	\N	f	1	\N	50	\N	\N	1
36	34	西城分所		\N	\N	1	2026-08-11 15:09:39.015643	1	1	12:00	80	0	30	0	15		0	7	3	1	50	0	0		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	03:00	1	0	0	0		auto_approve	queue_approve		\N			20		0	30	2026-08-17	0	\N	f	1	\N	30	\N	\N	1
32	32	玉溪第一考场		\N	\N	1	2026-08-10 12:49:18.884869	1	1	12:00	80	0	30	0	15		0	7	3	1	30	0	0		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	03:00	1	0	0	0		auto_approve	queue_approve		\N			20		0	30	2026-08-17	0	\N	f	1	\N	50	\N	\N	1
25	23	春天科目三		\N	\N	1	2026-08-01 11:30:28.285353	1	3	02:00	0	1	30	1	20		0	7	3	1	80	60	300		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	03:00	1	0	0	0		auto_approve	manual_approve		\N			20		0	30	2026-08-17	0	\N	f	1	\N	20	\N	\N	1
33	30	海岸考场		\N	\N	1	2026-08-10 12:49:50.230808	1	1	12:00	80	0	30	0	15		0	7	3	1	50	0	0		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	03:00	1	0	0	0		auto_approve	queue_approve		\N			20		0	30	2026-08-17	0	\N	f	1	\N	50	\N	\N	1
34	31	玉溪第二考场		\N	\N	1	2026-08-10 14:37:48.706447	1	1	12:00	80	0	30	0	15		0	7	3	1	70	0	0		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	03:00	1	0	0	0		auto_approve	queue_approve		\N			20		0	30	2026-08-17	0	\N	f	1	\N	50	\N	\N	1
26	24	夏邑大地考场		\N	\N	1	2026-08-01 11:32:41.688183	1	2	02:00	0	1	30	1	30		0	7	3	1	80	60	300		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	03:00	1	0	0	0		auto_approve	manual_approve		\N			20		0	30	2026-08-17	0	\N	f	1	\N	20	\N	\N	1
29	27	金堂		\N	\N	1	2026-08-03 12:57:59.604649	1	1	12:00	40	0	30	0	0		0	7	3	1	70	1	2		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	03:00	1	0	0	0		auto_approve	manual_approve		\N			20		0	30	2026-08-17	0	\N	f	1	\N	50	\N	\N	1
28	26	车管所		\N	\N	1	2026-08-03 12:55:10.931021	1	1	12:00	80	0	30	0	15		0	7	3	1	50	0	0		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	03:00	1	0	0	0		auto_approve	queue_approve		\N			20		0	30	2026-08-17	0	\N	f	1	\N	50	\N	\N	1
21	19	上黄科目二三		\N	\N	1	2026-07-29 07:04:26.113714	1	1	12:00	80	0	30	0	35		0	7	3	1	30	1	3		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	03:00	1	0	0	0		auto_approve	queue_approve		\N			20		0	30	2026-08-17	0	\N	f	1	\N	60	\N	\N	1
30	28	巴中宏阳考场		\N	\N	1	2026-08-04 16:52:37.222201	1	1	12:00	80	0	30	0	15		0	7	3	1	50	0	0		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	03:00	1	0	0	0		auto_approve	queue_approve		\N			20		0	30	2026-08-17	0	\N	f	1	\N	50	\N	\N	1
22	20	天元摩托车考场		\N	\N	1	2026-07-30 13:37:34.556935	1	1	12:00	80	1	30	0	15		0	7	3	1	50	1	2		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	03:00	1	0	0	0		auto_approve	queue_approve		\N			20		0	30	2026-08-17	0	\N	f	1	\N	50	\N	\N	1
20	18	林州考场		\N	\N	1	2026-07-26 11:10:00.129137	1	3	23:00	0	0	30	0	15		0	7	3	1	40	0	0	111	08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	03:00	1	0	0	0	11	auto_approve	manual_approve		\N			20		0	30	2026-08-17	0	\N	f	1	\N	30	\N	\N	1
38	36	大学城科目二		\N	\N	1	2026-08-13 11:05:31.499177	1	1	12:00	50	0	30	0	0		0	7	3	1	80	60	300		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	01:00	1	0	0	0		auto_approve	manual_approve		\N			20		0	30	2026-08-17	0	\N	f	1	\N	50	\N	\N	1
37	35	南陵考场		\N	\N	1	2026-08-13 09:26:15.727413	1	1	12:00	80	0	30	0	15		0	7	3	1	50	0	0		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	03:00	1	0	0	0		auto_approve	queue_approve		\N			20		0	30	2026-08-17	0	\N	f	1	\N	30	\N	\N	1
39	37	中信考场		\N	\N	1	2026-08-17 14:50:56.927607	1	1	12:00	80	3	30	1	0		0	7	3	1	80	60	300		08:00	22:00	0	auto	1	both	0	0	0		1	1		0		0	1	03:00	1	0	0	0		auto_approve	manual_approve		\N			20		0	30	\N	0	\N	f	1	\N	50		\N	1
12	9	元谷科目二		\N	\N	1	2026-07-03 09:14:28.174623	1	3	02:00	0	1	30	0	45		0	7	3	1	80	60	300		08:00	22:00	0	auto	1	both	1	0	0		1	1		0		0	1	23:00	1	0	0	0		auto_approve	manual_approve		\N			20		0	30	2026-08-17	0	\N	f	1	\N	50	\N	\N	1
9	6	运鑫科目三		\N	\N	1	2026-07-02 21:34:07.913709	1	3	02:00	0	1	5	0	25		0	7	3	1	80	60	300		06:00	00:00	0	auto	1	both	1	0	0		1	1		0		0	1	23:00	1	0	0	0		auto_approve	manual_approve		\N			20	wx57eaea52dcfff4e8	0	30	2026-08-17	0	\N	f	1	\N	30	\N	\N	1
\.


--
-- Name: locations_id_seq; Type: SEQUENCE SET; Schema: public; Owner: postgres
--

SELECT pg_catalog.setval('public.locations_id_seq', 39, true);


--
-- Name: locations locations_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY public.locations
    ADD CONSTRAINT locations_pkey PRIMARY KEY (id);


--
-- Name: TABLE locations; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE public.locations TO locker_admin;
GRANT SELECT ON TABLE public.locations TO replicator;


--
-- Name: SEQUENCE locations_id_seq; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON SEQUENCE public.locations_id_seq TO locker_admin;


--
-- PostgreSQL database dump complete
--

\unrestrict XgjOnfPHwqnusRksfDuIGhiZbcE6F20ZcW11IETaOBGdooXcv8VrggMEdY43LVP


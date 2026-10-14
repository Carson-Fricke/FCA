/** Read-only DOM snapshot of the linked conversation. Table numbers are display-rounded. */
export const source = {
	program: 'msft_multitf_trend_v4', job: 'job-dab2e2f8635f438fb9c036d3c8c3dd3b',
	version: '5db954e385c8', output: 'multitf_trend_summary',
	conversation: 'chat-aa414284d27047c1b003ca20b2f6af61'
};
export const rows = [
	['gap_cont','all','all',0,26.9392,1.78527,108,108],
	['gap_cont','gap_size','small',0,43.0423,2.00163,37,37],
	['gap_cont','gap_size','mid',0,35.8723,1.38508,36,36],
	['gap_cont','gap_size','large',0,0.727673,0.0235756,35,35],
	['gap_cont','er20','er_hi',0,37.1167,1.47428,30,30],
	['gap_cont','er20','er_lo',0,38.0358,1.33875,30,30],
	['trend_drift','er20','er20_hi',0,-6.48793,-0.248821,30,30],
	['trend_drift','er20','er20_lo',0,-14.9465,-0.512841,30,30],
	['trend_drift','er20','er20_mid',0,6.80416,0.217081,28,28],
	['stretch_cont','er20','er20_hi',5,-0.0196711,-0.062723,9750,30],
	['stretch_cont','er20','er20_hi',10,0.0770371,0.125873,9600,30],
	['stretch_cont','er20','er20_lo',5,-0.0538213,-0.167119,9750,30],
	['stretch_cont','er20','er20_lo',10,0.0159664,0.0265584,9600,30]
].map(([question,group,bucket,horizon,value_bp,t_session,n,n_sessions])=>({
	question:String(question),group:String(group),bucket:String(bucket),horizon:Number(horizon),
	value_bp:Number(value_bp),t_session:Number(t_session),n:Number(n),n_sessions:Number(n_sessions)
}));
export const metrics: Record<string,string> = {
	cagr:'0.000000000000000000',closed_trade_gross_pnl:'0E-18',closed_trade_net_pnl:'0E-18',
	daily_frames:'110',er20_max:'0.5793727908191068',er20_min:'0.005364898899702224',
	er20_q1:'0.10682052928131319',er20_q2:'0.31214815058591683',fees:'0E-18',fees_total:'0E-18',fill_count:'0',
	gap_abs_q1_bp:'50.77976850937757',gap_abs_q2_bp:'111.51540170594576',
	gap_cont_all_bp:'26.939224722727158',gap_cont_er_hi_bp:'37.11667813748247',gap_cont_er_lo_bp:'38.03578841148216',
	gap_cont_large_bp:'0.7276732243163175',gap_cont_mid_bp:'35.87234073292907',gap_cont_small_bp:'43.04225515724365',
	gross_loss:'0E-18',gross_pnl:'0E-18',gross_profit:'0E-18',intraday_frames:'42900',max_drawdown:'0E-18',
	net_loss:'0E-18',net_pnl:'0E-18',net_profit:'0E-18',open_fees:'0E-18',profit_factor_reason:'no_closed_trades',
	sessions_used:'109',sharpe_per_sample_reason:'zero_volatility',strategy_id:'msft_multitf_trend_v4',
	stretch_cont_er20_hi_h10_bp:'0.07703710879291462',stretch_cont_er20_hi_h5_bp:'-0.0196711205018603',
	stretch_cont_er20_lo_h10_bp:'0.015966431221223754',stretch_cont_er20_lo_h5_bp:'-0.053821282255839084',
	total_return:'0E-18',trade_count:'0',trend_drift_er20_hi_bp:'-6.487930575253641',
	trend_drift_er20_lo_bp:'-14.946460437100278',trend_drift_er20_mid_bp:'6.804164201446135'
};

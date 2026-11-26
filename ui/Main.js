Ext.define('SYNO.SDS.MSSQLBackup.Instance', {
    extend: 'SYNO.SDS.AppInstance',
    appWindowName: 'SYNO.SDS.MSSQLBackup.Main',
    constructor: function () {
        this.callParent(arguments);
    }
});

Ext.define('SYNO.SDS.MSSQLBackup.Main', {
    extend: 'SYNO.SDS.AppWindow',
    constructor: function (cfg) {
        cfg = cfg || {};
        this.appInstance = cfg.appInstance;
        var config = Ext.apply({
            resizable: true,
            maximizable: true,
            minimizable: true,
            width: 1100,
            height: 680,
            minWidth: 780,
            minHeight: 480,
            layout: 'fit',
            items: [
                new Ext.BoxComponent({
                    height: '100%',
                    html: '<iframe title="MSSQL Backup" src="/webman/3rdparty/MSSQLBackup/index.html?_ts=' + new Date().getTime() + '" frameborder="0" width="100%" height="100%" style="display:block;border:0;"></iframe>'
                })
            ]
        }, cfg);
        this.callParent([config]);
    }
});

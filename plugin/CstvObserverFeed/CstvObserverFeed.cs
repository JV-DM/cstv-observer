// CSTV Observer Feed — CounterStrikeSharp plugin for the CS2 game server.
//
// Streams real-time kill / damage / round events to the cstv-observer app so it can plan camera cuts
// BEFORE the (tv_delay-delayed) CSTV broadcast shows them. Only send this to a trusted observer
// machine: it is live information about the match.
//
// Config: addons/counterstrikesharp/configs/plugins/CstvObserverFeed/CstvObserverFeed.json
//   { "Url": "http://OBSERVER_PC:3500/feed", "Token": "change-me", "SendHurt": true, "FlushInterval": 0.25 }

using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using CounterStrikeSharp.API;
using CounterStrikeSharp.API.Core;
using CounterStrikeSharp.API.Core.Attributes.Registration;
using CounterStrikeSharp.API.Modules.Cvars;
using CounterStrikeSharp.API.Modules.Timers;

namespace CstvObserverFeed;

public class FeedConfig : BasePluginConfig
{
    [JsonPropertyName("Url")] public string Url { get; set; } = "http://127.0.0.1:3500/feed";
    [JsonPropertyName("Token")] public string Token { get; set; } = "change-me";
    [JsonPropertyName("SendHurt")] public bool SendHurt { get; set; } = true;
    [JsonPropertyName("FlushInterval")] public float FlushInterval { get; set; } = 0.25f;
}

public class CstvObserverFeed : BasePlugin, IPluginConfig<FeedConfig>
{
    public override string ModuleName => "CSTV Observer Feed";
    public override string ModuleVersion => "1.0.0";
    public override string ModuleAuthor => "cstv-observer";
    public override string ModuleDescription => "Real-time event feed for the cstv-observer lookahead director";

    public FeedConfig Config { get; set; } = new();

    private static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(3) };
    private readonly List<Dictionary<string, object?>> _queue = new();
    private readonly object _lock = new();
    private int _inFlight;

    public void OnConfigParsed(FeedConfig config)
    {
        Config = config;
    }

    public override void Load(bool hotReload)
    {
        if (hotReload)
            AddTimer(Math.Max(0.1f, Config.FlushInterval), Flush, TimerFlags.REPEAT | TimerFlags.STOP_ON_MAPCHANGE);
        RegisterListener<Listeners.OnMapStart>(_ =>
        {
            Enqueue("map_start", new());
            AddTimer(Math.Max(0.1f, Config.FlushInterval), Flush, TimerFlags.REPEAT | TimerFlags.STOP_ON_MAPCHANGE);
        });
    }

    // ------------------------------------------------------------------ helpers
    private static string? Sid(CCSPlayerController? p) =>
        p is { IsValid: true } && !p.IsBot && p.SteamID != 0 ? p.SteamID.ToString() : null;

    private static string? Name(CCSPlayerController? p) => p is { IsValid: true } ? p.PlayerName : null;

    private static int RoundNo()
    {
        try
        {
            var rules = Utilities.FindAllEntitiesByDesignerName<CCSGameRulesProxy>("cs_gamerules")
                .FirstOrDefault()?.GameRules;
            return rules?.TotalRoundsPlayed ?? -1;
        }
        catch { return -1; }
    }

    private static float TvDelay()
    {
        var cv = ConVar.Find("tv_delay");
        if (cv == null) return 0f;
        try { return cv.GetPrimitiveValue<float>(); } catch { }
        try { return cv.GetPrimitiveValue<int>(); } catch { }
        return float.TryParse(cv.StringValue, System.Globalization.NumberStyles.Float,
            System.Globalization.CultureInfo.InvariantCulture, out var v) ? v : 0f;
    }

    private void Enqueue(string type, Dictionary<string, object?> data)
    {
        data["type"] = type;
        data["t"] = Server.CurrentTime;
        data["tick"] = Server.TickCount;
        data["round"] = RoundNo();
        lock (_lock) _queue.Add(data);
    }

    private void Flush()
    {
        List<Dictionary<string, object?>> batch;
        lock (_lock)
        {
            batch = new List<Dictionary<string, object?>>(_queue);
            _queue.Clear();
        }
        // a heartbeat with an empty event list is still useful: it keeps the clock estimate fresh
        var payload = new Dictionary<string, object?>
        {
            ["now"] = Server.CurrentTime,
            ["tick"] = Server.TickCount,
            ["tv_delay"] = TvDelay(),
            ["map"] = Server.MapName,
            ["events"] = batch,
        };
        if (Interlocked.CompareExchange(ref _inFlight, 1, 0) != 0)
        {
            lock (_lock) _queue.InsertRange(0, batch);  // previous POST still running: retry next flush
            return;
        }
        var json = JsonSerializer.Serialize(payload);
        var url = Config.Url;
        var token = Config.Token;
        Task.Run(async () =>
        {
            try
            {
                using var req = new HttpRequestMessage(HttpMethod.Post, url)
                {
                    Content = new StringContent(json, Encoding.UTF8, "application/json"),
                };
                req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
                using var resp = await Http.SendAsync(req);
            }
            catch (Exception)
            {
                // observer offline: drop silently (events are only useful in real time)
            }
            finally
            {
                Interlocked.Exchange(ref _inFlight, 0);
            }
        });
    }

    // ------------------------------------------------------------------ events
    [GameEventHandler]
    public HookResult OnPlayerDeath(EventPlayerDeath ev, GameEventInfo info)
    {
        Enqueue("death", new()
        {
            ["victim"] = Sid(ev.Userid), ["victim_name"] = Name(ev.Userid),
            ["attacker"] = Sid(ev.Attacker), ["attacker_name"] = Name(ev.Attacker),
            ["weapon"] = ev.Weapon, ["headshot"] = ev.Headshot,
        });
        return HookResult.Continue;
    }

    [GameEventHandler]
    public HookResult OnPlayerHurt(EventPlayerHurt ev, GameEventInfo info)
    {
        if (!Config.SendHurt) return HookResult.Continue;
        Enqueue("hurt", new()
        {
            ["victim"] = Sid(ev.Userid), ["victim_name"] = Name(ev.Userid),
            ["attacker"] = Sid(ev.Attacker), ["attacker_name"] = Name(ev.Attacker),
            ["dmg"] = ev.DmgHealth,
        });
        return HookResult.Continue;
    }

    [GameEventHandler]
    public HookResult OnFreezeEnd(EventRoundFreezeEnd ev, GameEventInfo info)
    {
        Enqueue("freeze_end", new());
        return HookResult.Continue;
    }

    [GameEventHandler]
    public HookResult OnBombPlanted(EventBombPlanted ev, GameEventInfo info)
    {
        Enqueue("bomb_planted", new() { ["player"] = Sid(ev.Userid) });
        return HookResult.Continue;
    }

    [GameEventHandler]
    public HookResult OnRoundEnd(EventRoundEnd ev, GameEventInfo info)
    {
        Enqueue("round_end", new() { ["winner"] = ev.Winner });
        return HookResult.Continue;
    }
}

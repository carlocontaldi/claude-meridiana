# Claude Meridiana

> **me·ri·dia·na** · Italian /me.riˈdja.na/ (*meh-ree-DYAH-nah*) · *noun*, feminine, plural *meridiane*
>
> A sundial: a dial on which the shadow of a rod, the gnomon, shows the hour.

A sundial reads the hour from a shadow. Claude Meridiana reads a Claude Code usage-limit error and works out when each cap resets. It is the code that [Claude Portavoce](https://github.com/carlocontaldi/claude-portavoce) and [Claude Goon](https://github.com/carlocontaldi/claude-goon) share: which caps the error reports and when each comes back, a flock-based claim so one watcher runs per reset time, and the ntfy push.

Python 3.9+, standard library only. It is not a package or a plugin: a plugin is cloned on its own and cannot import from another repository, so `sync.py` copies `limitwatch.py` into each plugin's `hooks/` with a hash header, and each plugin's tests fail if its copy no longer matches that header.

```
python3 sync.py           # copy into ../claude-portavoce/hooks and ../claude-goon/hooks
python3 sync.py --check   # exit 1 if a copy differs from limitwatch.py
python3 -m unittest discover -s tests
```

## Caps it knows

Read from the error row's `quotaLimits` / `apiErrorParams.rate_limit_info`, else from the message text.

| `rateLimitType` | Label |
|---|---|
| `five_hour` | 5-hour limit |
| `seven_day` | weekly limit |
| `seven_day_opus` / `seven_day_sonnet` | Opus limit / Sonnet limit |
| `seven_day_overage_included` | Fable limit |
| `overage` | usage credit limit |

A spend cap that blocks the usage credits is reported beside the limit the error names, with the reset from `overageResetsAt`: `overageDisabledReason` `org_spend_cap_reached` is a spend limit, `org_level_disabled_until` the monthly one. Claude Code's error does not say whether a spend cap is daily, weekly or monthly, so the period is inferred, in this order:

1. the error's own flags: `dailyCapPaused` is daily, a full `overagePeriodMonthly` is monthly;
2. a reset more than a week away, or at the turn of a month (00:00 UTC on the 1st), is monthly;
3. a reset between one day and a week away is weekly;
4. a reset within a day could be a daily or a weekly cap, so it stays "individual spend limit".

Rules 2 and 3 assume a monthly cap resets at the turn of the calendar month. These kinds only limit the credits, so a plugin can tell that the plan itself is usable once its window resets.

The spend-cap and Fable rows come from Claude Code 2.1.322's own tables, not from a real error: every limit row seen so far is a 5-hour or weekly one.

## Private ntfy topics

`stored_topic(path, prefix)` makes `<prefix>-` and 22 random letters and digits (131 bits, from `secrets`) on first use, saves it at `path` (mode 600) and returns it from then on; `effective_topic(option, path, prefix)` lets a plugin option win, with an empty option meaning no push. A plugin's `topic` command shows the name to subscribe the phone app to.

## License

[MIT](LICENSE)

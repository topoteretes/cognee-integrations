package ai.cognee.demo;

import android.app.Activity;
import android.graphics.Color;
import android.graphics.Typeface;
import android.os.Bundle;
import android.view.Gravity;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashSet;

/**
 * Cognee memory, on a phone: opens straight onto the answer to one question
 * asked of the desktop knowledge graph. Tries the live backend first (pair
 * the device with `adb reverse tcp:8765 tcp:8765`, or set BACKEND to the
 * Mac's LAN IP); falls back to the answer bundled in assets/answer.txt, so
 * the demo never opens onto an empty screen.
 */
public class MainActivity extends Activity {

    // With `adb reverse tcp:8765 tcp:8765`, 127.0.0.1 on the phone IS the
    // Mac's backend. On Wi-Fi instead, set this to e.g. "http://192.168.1.20:8765".
    private static final String BACKEND = "http://127.0.0.1:8765";
    private static final String QUESTION = "what lenovo competitors there are";
    private static final int PURPLE = Color.rgb(139, 92, 255); // cognee accent

    private TextView status;
    private TextView body;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.rgb(16, 14, 24));
        int pad = (int) (20 * getResources().getDisplayMetrics().density);
        root.setPadding(pad, pad * 2, pad, pad);

        TextView brand = new TextView(this);
        brand.setText("cognee");
        brand.setTextColor(PURPLE);
        brand.setTextSize(28);
        brand.setTypeface(Typeface.DEFAULT_BOLD);
        root.addView(brand);

        TextView question = new TextView(this);
        question.setText("“" + QUESTION + "”");
        question.setTextColor(Color.rgb(160, 155, 175));
        question.setTextSize(15);
        question.setPadding(0, pad / 3, 0, pad / 2);
        root.addView(question);

        status = new TextView(this);
        status.setText("asking your memory…");
        status.setTextColor(Color.rgb(120, 115, 135));
        status.setTextSize(12);
        status.setGravity(Gravity.START);
        root.addView(status);

        body = new TextView(this);
        body.setTextColor(Color.rgb(235, 232, 245));
        body.setTextSize(16);
        body.setLineSpacing(0, 1.25f);
        body.setPadding(0, pad / 2, 0, pad);

        ScrollView scroll = new ScrollView(this);
        scroll.addView(body);
        root.addView(scroll);

        setContentView(root);

        // bundled copy first (instant screen), live answer replaces it
        body.setText(readAsset());
        status.setText("bundled copy · connect the backend for live memory");
        new Thread(this::fetchLive).start();
    }

    private void fetchLive() {
        try {
            String url = BACKEND + "/search?mode=answer&q="
                    + URLEncoder.encode(QUESTION, "UTF-8");
            HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
            conn.setConnectTimeout(4000);
            conn.setReadTimeout(120000);
            StringBuilder raw = new StringBuilder();
            try (BufferedReader reader = new BufferedReader(
                    new InputStreamReader(conn.getInputStream(), StandardCharsets.UTF_8))) {
                for (String line; (line = reader.readLine()) != null; ) raw.append(line);
            }
            JSONObject json = new JSONObject(raw.toString());
            String answer = json.optString("answer", "");
            if (answer.isEmpty() || "null".equals(answer)) return; // keep the bundle

            LinkedHashSet<String> layers = new LinkedHashSet<>();
            JSONArray sources = json.optJSONArray("sources");
            if (sources != null) {
                for (int i = 0; i < sources.length(); i++) {
                    layers.add(sources.getJSONObject(i).optString("layer"));
                }
            }
            String attribution = layers.isEmpty()
                    ? "live from your knowledge graph"
                    : "live · from " + String.join(" + ", layers);

            String clean = answer.replace("**", "");
            runOnUiThread(() -> {
                body.setText(clean);
                status.setText(attribution);
                status.setTextColor(PURPLE);
            });
        } catch (Exception ignored) {
            // backend unreachable: the bundled copy is already on screen
        }
    }

    private String readAsset() {
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(
                getAssets().open("answer.txt"), StandardCharsets.UTF_8))) {
            StringBuilder text = new StringBuilder();
            for (String line; (line = reader.readLine()) != null; ) {
                text.append(line).append('\n');
            }
            return text.toString();
        } catch (Exception e) {
            return "No bundled answer found.";
        }
    }
}

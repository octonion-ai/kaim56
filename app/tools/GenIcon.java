import java.awt.Color;
import java.awt.Font;
import java.awt.FontMetrics;
import java.awt.Graphics2D;
import java.awt.RenderingHints;
import java.awt.geom.Ellipse2D;
import java.awt.image.BufferedImage;
import java.io.File;
import javax.imageio.ImageIO;

/** Erzeugt das KatAgent-Launcher-Icon: "56" (weiss) auf blauem Kreis. */
public class GenIcon {
    static final Color BG = new Color(0x2F, 0x6F, 0xED); // Signal-artiges Blau

    public static void main(String[] args) throws Exception {
        String res = args[0];
        String[] d = {"mdpi", "hdpi", "xhdpi", "xxhdpi", "xxxhdpi"};
        int[] fg = {108, 162, 216, 324, 432};   // adaptive foreground
        int[] leg = {48, 72, 96, 144, 192};      // legacy launcher
        for (int i = 0; i < d.length; i++) {
            drawForeground(res, d[i], fg[i]);
            drawLegacy(res, d[i], leg[i]);
        }
        System.out.println("Icons erzeugt.");
    }

    static Graphics2D g2(BufferedImage img) {
        Graphics2D g = img.createGraphics();
        g.setRenderingHint(RenderingHints.KEY_ANTIALIASING, RenderingHints.VALUE_ANTIALIAS_ON);
        g.setRenderingHint(RenderingHints.KEY_TEXT_ANTIALIASING, RenderingHints.VALUE_TEXT_ANTIALIAS_ON);
        return g;
    }

    static void draw56(Graphics2D g, int size, float scale) {
        g.setColor(Color.WHITE);
        g.setFont(new Font("SansSerif", Font.BOLD, Math.round(size * scale)));
        FontMetrics fm = g.getFontMetrics();
        String s = "56";
        int tw = fm.stringWidth(s);
        int asc = fm.getAscent(), desc = fm.getDescent();
        int x = (size - tw) / 2;
        int y = (size - (asc + desc)) / 2 + asc;
        g.drawString(s, x, y);
    }

    static void drawForeground(String res, String d, int size) throws Exception {
        BufferedImage img = new BufferedImage(size, size, BufferedImage.TYPE_INT_ARGB);
        Graphics2D g = g2(img);
        draw56(g, size, 0.42f);   // zentriert in der Safe-Zone; Hintergrund liefert die Farbe
        g.dispose();
        File dir = new File(res, "mipmap-" + d);
        dir.mkdirs();
        ImageIO.write(img, "png", new File(dir, "ic_launcher_foreground.png"));
    }

    static void drawLegacy(String res, String d, int size) throws Exception {
        BufferedImage img = new BufferedImage(size, size, BufferedImage.TYPE_INT_ARGB);
        Graphics2D g = g2(img);
        g.setColor(BG);
        g.fill(new Ellipse2D.Float(0, 0, size, size));
        draw56(g, size, 0.5f);
        g.dispose();
        File dir = new File(res, "mipmap-" + d);
        dir.mkdirs();
        ImageIO.write(img, "png", new File(dir, "ic_launcher.png"));
        ImageIO.write(img, "png", new File(dir, "ic_launcher_round.png"));
    }
}

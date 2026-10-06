package com.onedayonemasterpiece.streetstory;

import android.graphics.Bitmap;
import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;

/** Pure Android/Java so the standalone test-provider process needs no app dependencies. */
public final class PhotoGpsFixture {
    private PhotoGpsFixture() {}

    public static byte[] bytes() {
        return bytes(24, 32);
    }

    public static byte[] galleryBytes() {
        return bytes(480, 640);
    }

    private static byte[] bytes(int width, int height) {
        Bitmap bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888);
        ByteArrayOutputStream jpegOutput = new ByteArrayOutputStream();
        bitmap.compress(Bitmap.CompressFormat.JPEG, 90, jpegOutput);
        bitmap.recycle();
        byte[] jpeg = jpegOutput.toByteArray();
        ByteBuffer tiff = ByteBuffer.allocate(140).order(ByteOrder.LITTLE_ENDIAN);
        tiff.put((byte) 'I').put((byte) 'I').putShort((short) 42).putInt(8);
        tiff.putShort((short) 2);
        entry(tiff, 0x112, 3, 1, 6);
        entry(tiff, 0x8825, 4, 1, 38);
        tiff.putInt(0).putShort((short) 4);
        entry(tiff, 1, 2, 2, 'N');
        entry(tiff, 2, 5, 3, 92);
        entry(tiff, 3, 2, 2, 'E');
        entry(tiff, 4, 5, 3, 116);
        tiff.putInt(0);
        for (int value : new int[]{54, 1, 42, 1, 4444416, 1000000, 20, 1, 30, 1, 8444412, 1000000}) {
            tiff.putInt(value);
        }
        byte[] exif = new byte[146];
        exif[0] = 'E'; exif[1] = 'x'; exif[2] = 'i'; exif[3] = 'f';
        System.arraycopy(tiff.array(), 0, exif, 6, 140);
        byte[] source = new byte[6 + exif.length + jpeg.length - 2];
        source[0] = (byte) 0xff; source[1] = (byte) 0xd8;
        source[2] = (byte) 0xff; source[3] = (byte) 0xe1;
        source[4] = (byte) ((exif.length + 2) >> 8); source[5] = (byte) (exif.length + 2);
        System.arraycopy(exif, 0, source, 6, exif.length);
        System.arraycopy(jpeg, 2, source, 6 + exif.length, jpeg.length - 2);
        return source;
    }

    private static void entry(ByteBuffer buffer, int tag, int type, int count, int value) {
        buffer.putShort((short) tag).putShort((short) type).putInt(count).putInt(value);
    }
}
